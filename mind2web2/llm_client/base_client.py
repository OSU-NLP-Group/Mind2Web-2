"""Chat Completions client for OpenAI, Azure OpenAI, and OpenAI-compatible servers."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any

import openai
import pydantic

from .judge import ContextLengthError, JudgeConfig, JudgeContentError, JudgeError

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PROVIDERS = ("openai", "azure_openai")
DEFAULT_RETRY_SECONDS = 900.0
DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_RETRY_DELAY_SECONDS = 120.0
MIN_ATTEMPT_SECONDS = 1.0

_REQUEST_ERRORS = (openai.OpenAIError, pydantic.ValidationError, json.JSONDecodeError)
_JUDGE_PARAMS = ("model", "reasoning_effort", "temperature")
#: Error codes of HTTP 400 responses that reject a request because of its content.
CONTENT_REJECTION_CODES = ("content_filter", "content_policy_violation", "invalid_prompt")


class LLMClient:
    """Sends Chat Completions requests, parsing structured output when ``response_format`` is a Pydantic model.

    ``provider`` is ``"openai"`` or ``"azure_openai"``.  The OpenAI provider reads
    ``OPENAI_API_KEY`` and sends requests to ``base_url``, or to
    ``OPENAI_BASE_URL`` when ``base_url`` is not given, so any OpenAI-compatible
    server (for example a LiteLLM proxy) can serve them.  The Azure provider
    reads ``AZURE_OPENAI_API_KEY``, ``AZURE_OPENAI_ENDPOINT_URL``, and
    ``AZURE_OPENAI_API_VERSION``.

    With ``judge`` set, every request goes to ``judge.model`` with the judge's
    parameters, replacing any ``model``, ``reasoning_effort``, or ``temperature``
    the caller passes; without it, requests are sent as given.

    Transient failures are retried with jittered exponential backoff that starts
    at ``retry_initial_delay`` seconds and honors the server's ``retry-after``
    header, until ``retry_seconds`` have passed since the first attempt.  They
    are the failures the OpenAI SDK itself retries: connection errors and
    timeouts, and HTTP 408, 409, 429, and 5xx responses, unless the response's
    ``x-should-retry`` header says otherwise; exhausted quota (HTTP 429 with
    code ``insufficient_quota``) is not retried.  A failure that is not retried,
    or that outlasts the retry budget, raises :class:`JudgeError`.  A rejection
    of the request's content raises its subclass :class:`JudgeContentError`: a
    refusal, a response without the requested structured output, an output cut
    off at the token limit, a response stopped by the content filter, and HTTP
    400 with one of :data:`CONTENT_REJECTION_CODES`.  HTTP 400 with code
    ``context_length_exceeded`` raises :class:`ContextLengthError`.

    The first attempt may take up to ``timeout`` seconds, and each retry's
    timeout is cut to the time left in the budget, so a request takes about
    ``max(timeout, retry_seconds)`` in total at most: 15 minutes with the
    defaults (the SDK applies the timeout to each phase of an attempt, such as
    connecting and reading, so one attempt can run somewhat longer).  Once one
    request has spent its whole budget unable to connect to the server (the
    connection is refused or the host name does not resolve; a connection
    attempt that times out counts as a timeout, not as unable to connect), later requests that cannot connect fail after their first attempt,
    until a request succeeds again, so that an unreachable endpoint does not
    hold every request for the full budget.

    ``response(**kwargs)`` / ``async_response(**kwargs)`` take Chat Completions
    parameters and return the parsed Pydantic object (structured output) or the
    message text; with ``count_token=True`` they return ``(result, tokens)``
    where ``tokens`` has ``input_tokens``, ``cached_input_tokens``,
    ``output_tokens``, ``reasoning_tokens``, and ``served_model`` (the model
    the server reports serving the request, when it reports one).
    """

    def __init__(
            self,
            provider: str = "openai",
            is_async: bool = False,
            *,
            judge: JudgeConfig | None = None,
            base_url: str | None = None,
            retry_seconds: float = DEFAULT_RETRY_SECONDS,
            retry_initial_delay: float = 1.0,
            timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"Provider {provider!r} not supported; choose one of {PROVIDERS}")
        self.provider = provider
        self.is_async = is_async
        self.judge = judge
        self.retry_seconds = retry_seconds
        self.retry_initial_delay = retry_initial_delay
        self.timeout = timeout
        self.client = _sdk_client(provider, is_async, base_url, timeout)
        self._overridden_models: set[str] = set()
        self._unreachable = False  # a request spent its whole retry budget unable to connect

    def response(self, count_token: bool = False, **kwargs: Any) -> Any:
        if self.is_async:
            raise ValueError("This client is async; use async_response()")
        request, structured = self._prepare(kwargs)
        completions = self.client.chat.completions
        call = completions.parse if structured else completions.create
        budget = _RetryBudget(self.retry_seconds, self.retry_initial_delay)
        options: dict[str, Any] = {}
        while True:
            try:
                completion = call(**request, **options)
                break
            except _REQUEST_ERRORS as exc:
                wait = self._next_wait(budget, exc)
                if wait is None:
                    raise _failure(request, exc) from exc
                _log_retry(request, exc, wait)
                time.sleep(wait)
                options = {"timeout": budget.attempt_timeout(self.timeout)}
        self._unreachable = False
        return _unpack(completion, request, structured, count_token)

    async def async_response(self, count_token: bool = False, **kwargs: Any) -> Any:
        if not self.is_async:
            raise ValueError("This client is synchronous; use response()")
        request, structured = self._prepare(kwargs)
        completions = self.client.chat.completions
        call = completions.parse if structured else completions.create
        budget = _RetryBudget(self.retry_seconds, self.retry_initial_delay)
        options: dict[str, Any] = {}
        while True:
            try:
                completion = await call(**request, **options)
                break
            except _REQUEST_ERRORS as exc:
                wait = self._next_wait(budget, exc)
                if wait is None:
                    raise _failure(request, exc) from exc
                _log_retry(request, exc, wait)
                await asyncio.sleep(wait)
                options = {"timeout": budget.attempt_timeout(self.timeout)}
        self._unreachable = False
        return _unpack(completion, request, structured, count_token)

    def _next_wait(self, budget: _RetryBudget, exc: BaseException) -> float | None:
        """Seconds to wait before retrying after ``exc``, or ``None`` to give up.

        A failure to connect (not a timeout) gives up at once while the server
        is marked unreachable, and marks it so when it exhausts the budget.
        """
        cannot_connect = isinstance(exc, openai.APIConnectionError) and not isinstance(exc, openai.APITimeoutError)
        if cannot_connect and self._unreachable:
            return None
        wait = budget.next_wait(exc)
        if wait is None and cannot_connect:
            self._unreachable = True
            logger.error("The server could not be reached for the whole retry budget; later requests that "
                         "cannot connect fail without retrying until a request succeeds")
        return wait

    def _prepare(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Apply the judge configuration and decide between structured and plain requests."""
        request = dict(kwargs)
        if self.judge is not None:
            requested = request.get("model")
            if requested is not None and requested != self.judge.model and requested not in self._overridden_models:
                self._overridden_models.add(requested)
                logger.warning("Requests for model %r are sent to the judge model %r", requested, self.judge.model)
            for key in _JUDGE_PARAMS:
                request.pop(key, None)
            request.update(self.judge.request_params())
        if not request.get("model"):
            raise ValueError("No model given and no judge configured")
        response_format = request.get("response_format")
        structured = isinstance(response_format, type) and issubclass(response_format, pydantic.BaseModel)
        return request, structured


class _RetryBudget:
    """Wait times and timeouts for retrying one request, within a total time budget."""

    def __init__(self, seconds: float, initial_delay: float) -> None:
        self.deadline = time.monotonic() + seconds
        self.delay = initial_delay

    def next_wait(self, exc: BaseException) -> float | None:
        """Seconds to wait before retrying after ``exc``, or ``None`` to give up.

        Gives up when ``exc`` is not transient, and when less than
        ``MIN_ATTEMPT_SECONDS`` of the budget would be left for the retry.
        """
        if not _is_transient(exc):
            return None
        wait = max(self.delay * (0.5 + random.random()), _retry_after(exc) or 0.0)
        wait = min(wait, MAX_RETRY_DELAY_SECONDS)
        if time.monotonic() + wait + MIN_ATTEMPT_SECONDS > self.deadline:
            return None
        self.delay = min(self.delay * 2, MAX_RETRY_DELAY_SECONDS)
        return wait

    def attempt_timeout(self, timeout: float) -> float:
        """The timeout of a retry: ``timeout``, cut to the time left in the budget."""
        return max(MIN_ATTEMPT_SECONDS, min(timeout, self.deadline - time.monotonic()))


def _is_transient(exc: BaseException) -> bool:
    """Whether a failed request may succeed if sent again.

    Connection errors and timeouts always may.  For an HTTP error response,
    an ``x-should-retry`` header of ``true`` or ``false`` decides; without it,
    408, 409, 429, and 5xx responses may.  Exhausted quota (a 429 with code
    ``insufficient_quota``) and every other failure may not.
    """
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return True
    if not isinstance(exc, openai.APIStatusError) or exc.code == "insufficient_quota":
        return False
    should_retry = exc.response.headers.get("x-should-retry")
    if should_retry in ("true", "false"):
        return should_retry == "true"
    return exc.status_code in (408, 409, 429) or exc.status_code >= 500


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = response.headers
    try:
        if "retry-after-ms" in headers:
            return float(headers["retry-after-ms"]) / 1000
        if "retry-after" in headers:
            return float(headers["retry-after"])
    except ValueError:
        return None
    return None


def _sdk_client(provider: str, is_async: bool, base_url: str | None, timeout: float):
    # The SDK's own retries are disabled: LLMClient retries within its time budget instead.
    common = {"timeout": timeout, "max_retries": 0}
    if provider == "openai":
        cls = openai.AsyncOpenAI if is_async else openai.OpenAI
        return cls(base_url=base_url, **common)
    if base_url is not None:
        raise ValueError("A base URL applies only to the openai provider; Azure OpenAI takes its endpoint from "
                         "AZURE_OPENAI_ENDPOINT_URL")
    cls = openai.AsyncAzureOpenAI if is_async else openai.AzureOpenAI
    return cls(
        api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT_URL"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        **common,
    )


def _failure(request: dict[str, Any], exc: BaseException) -> JudgeError:
    """The :class:`JudgeError` for a request that failed for good with ``exc``, of the class its cause calls for."""
    message = f"Request to {request.get('model')} failed: {type(exc).__name__}: {exc}"
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 400:
        if exc.code == "context_length_exceeded":
            return ContextLengthError(message)
        if exc.code in CONTENT_REJECTION_CODES:
            return JudgeContentError(message)
    if isinstance(exc, (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError)):
        return JudgeContentError(message)
    return JudgeError(message)


def _log_retry(request: dict[str, Any], exc: BaseException, wait: float) -> None:
    logger.warning("Retrying request to %s in %.1fs after %s: %s",
                   request.get("model"), wait, type(exc).__name__, exc)


def _unpack(completion: Any, request: dict[str, Any], structured: bool, count_token: bool) -> Any:
    if not completion.choices:
        raise JudgeError(f"{request.get('model')} returned no choices")
    message = completion.choices[0].message
    if structured:
        content = message.parsed
        if content is None:
            refusal = getattr(message, "refusal", None)
            raise JudgeContentError(f"{request.get('model')} returned no structured output"
                                    + (f" (refusal: {refusal})" if refusal else ""))
    else:
        content = message.content
    if not count_token:
        return content
    tokens: dict[str, Any] = _token_counts(completion.usage)
    served_model = getattr(completion, "model", None)
    if served_model:
        tokens["served_model"] = served_model
    return content, tokens


def _token_counts(usage: Any) -> dict[str, int]:
    """Token counts of one completion, with 0 for the counts the server omits."""
    if usage is None:  # some OpenAI-compatible servers omit usage
        return {}
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "input_tokens": usage.prompt_tokens or 0,
        "cached_input_tokens": getattr(prompt_details, "cached_tokens", None) or 0,
        "output_tokens": usage.completion_tokens or 0,
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None) or 0,
    }
