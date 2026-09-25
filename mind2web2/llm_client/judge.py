"""The judge: which model scores answers, how its requests are parameterized, and how they are accounted.

One judge scores every answer of an evaluation run.  :class:`JudgeConfig` names
the model and its request parameters; :class:`~mind2web2.llm_client.LLMClient`
sends every request to that model, whatever model an eval script names, so a
run never mixes judges.  :class:`JudgeUsage` accumulates the requests made for
one answer.  :class:`JudgeError` marks a request that failed for good, and its
subclass :class:`JudgeContentError` a request that the judge rejected because
of what it contains.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_JUDGE_MODEL = "gpt-6-luna"
DEFAULT_JUDGE_REASONING_EFFORT = "max"


@dataclass(frozen=True)
class JudgeConfig:
    """The judge model and the request parameters it is called with.

    ``reasoning_effort`` and ``temperature`` are sent only when set; ``None``
    keeps the model's default.  Reasoning models such as ``gpt-6-luna`` and
    ``o4-mini`` accept ``reasoning_effort`` and reject ``temperature``;
    non-reasoning models such as ``gpt-4.1`` are the other way around.

    The default judge, :data:`DEFAULT_JUDGE_MODEL`, always reasons at
    :data:`DEFAULT_JUDGE_REASONING_EFFORT`: a ``None`` ``reasoning_effort``
    with that model becomes ``"max"``, so it is never called at its own,
    lower default.  Any other model gets no ``reasoning_effort`` unless one
    is given.
    """

    model: str = DEFAULT_JUDGE_MODEL
    reasoning_effort: str | None = None
    temperature: float | None = None

    def __post_init__(self):
        if self.reasoning_effort is None and self.model == DEFAULT_JUDGE_MODEL:
            object.__setattr__(self, "reasoning_effort", DEFAULT_JUDGE_REASONING_EFFORT)

    def request_params(self) -> dict[str, Any]:
        """The Chat Completions parameters that select and configure the judge."""
        params: dict[str, Any] = {"model": self.model}
        if self.reasoning_effort is not None:
            params["reasoning_effort"] = self.reasoning_effort
        if self.temperature is not None:
            params["temperature"] = self.temperature
        return params

    def describe(self) -> dict[str, Any]:
        """JSON-serializable form, recorded in every evaluation result."""
        return asdict(self)


class JudgeError(RuntimeError):
    """A judge request failed for good, so the answer it was made for cannot be scored.

    Raised after transient failures (connection errors, timeouts, rate limits,
    server errors, and other responses marked as retryable) have been retried
    until the client's retry budget ran out, and immediately for any other
    failure of the judge's availability or configuration: invalid request,
    authentication, unknown model, exhausted quota.  Evaluation code lets it
    propagate instead of scoring the verification as failed, so that a judge
    outage or a configuration mistake never lowers an agent's score.  The
    answer is left without a result and is evaluated again on the next run.

    The subclass :class:`JudgeContentError` is the exception: it is caused by
    one request's content, recurs whenever that request is sent, and is
    confined to the check that made the request.
    """


class JudgeContentError(JudgeError):
    """The judge rejected one request because of its content, so resending it would fail again.

    Raised for a content-filter or usage-policy rejection (HTTP 400 with code
    ``content_filter``, ``content_policy_violation``, or ``invalid_prompt``, or
    a response that stopped at the content filter), an output cut off at the
    token limit, and a response without the requested structured output, which
    includes a refusal to answer a structured request.  Evaluation scores the
    check that made the request as failed (a failed vote under majority voting,
    empty values for an extraction), records the rejection in the answer's
    :class:`JudgeUsage`, and scores the answer as usual; the metrics list the
    answers with rejected requests.  It does not count as a failed request, so
    the answer's later requests are still sent.
    """


class ContextLengthError(JudgeContentError):
    """A request exceeded the judge model's context length (HTTP 400 with code ``context_length_exceeded``).

    Evaluation sends a request that contains a page again with the page text
    cut to half its token budget, up to twice, before treating it as a
    rejection like any other :class:`JudgeContentError`.
    """


@dataclass
class JudgeUsage:
    """Judge requests made while evaluating one answer, with their token usage.

    ``served_models`` counts the successful requests per model name that the
    server reported in its responses.  It can differ from the configured
    model: an alias such as ``gpt-6-luna`` names a dated snapshot, an Azure
    deployment name can point at any model, and a server behind
    ``--judge-base-url`` may map names.  Servers that report no model are not
    counted.

    ``rejections`` lists the requests the judge rejected because of their
    content (:class:`JudgeContentError`), each as ``{"check", "url",
    "reason"}``: the operation that made the request, the page it contained
    (``None`` for a request without one), and the error message.  Rejected
    requests are not in ``requests`` or ``failed_requests``.
    """

    requests: int = 0
    failed_requests: int = 0
    first_failure: str | None = None  # "<error type>: <message>" of the first failed request; not in as_dict()
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    served_models: dict[str, int] = field(default_factory=dict)
    rejections: list[dict[str, Any]] = field(default_factory=list)

    def record(self, tokens: dict[str, Any]) -> None:
        """Add one successful request with the token counts and served model returned by the client."""
        self.requests += 1
        self.input_tokens += tokens.get("input_tokens", 0)
        self.cached_input_tokens += tokens.get("cached_input_tokens", 0)
        self.output_tokens += tokens.get("output_tokens", 0)
        self.reasoning_tokens += tokens.get("reasoning_tokens", 0)
        served_model = tokens.get("served_model")
        if served_model:
            self.served_models[served_model] = self.served_models.get(served_model, 0) + 1

    def record_rejection(self, check: str, url: str | None, error: JudgeContentError) -> None:
        """Add one request that the judge rejected because of its content."""
        self.rejections.append({"check": check, "url": url, "reason": str(error)})

    def as_dict(self) -> dict[str, Any]:
        """The usage as saved in a result, without ``first_failure``: a result is saved only when no request failed."""
        data = asdict(self)
        del data["first_failure"]
        return data
