"""LLMClient: judge enforcement, request parameters, retries, and failure reporting.

The OpenAI SDK's ``chat.completions`` object is replaced by a scripted fake, so
no request leaves the process.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
from types import SimpleNamespace

import openai
import pytest
from pydantic import BaseModel

from mind2web2.llm_client import DEFAULT_JUDGE_MODEL, JudgeConfig, JudgeError, LLMClient

# openai>=3 builds on httpx2; earlier releases on httpx.
hx = importlib.import_module("httpx2" if importlib.util.find_spec("httpx2") else "httpx")
REQUEST = hx.Request("POST", "https://api.openai.com/v1/chat/completions")


class Verdict(BaseModel):
    reasoning: str
    result: bool


def status_error(cls, status: int, headers: dict | None = None, code: str | None = None):
    response = hx.Response(status, request=REQUEST, headers=headers or {})
    return cls(f"HTTP {status}", response=response, body={"code": code} if code else None)


def completion(parsed=None, content=None, refusal=None):
    usage = SimpleNamespace(
        prompt_tokens=10, completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
    )
    message = SimpleNamespace(parsed=parsed, content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class ScriptedCompletions:
    """Returns (or raises) the given outcomes in order and records every call."""

    def __init__(self, *outcomes, is_async: bool = True):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []
        self.is_async = is_async

    def _next(self, method: str, kwargs: dict):
        self.calls.append((method, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def __getattr__(self, method: str):
        if method not in ("parse", "create"):
            raise AttributeError(method)
        if self.is_async:
            async def call(**kwargs):
                return self._next(method, kwargs)
        else:
            def call(**kwargs):
                return self._next(method, kwargs)
        return call


def make_client(completions: ScriptedCompletions, monkeypatch, **kwargs) -> LLMClient:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = LLMClient("openai", is_async=completions.is_async, retry_initial_delay=0.001, **kwargs)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client


MESSAGES = [{"role": "user", "content": "Is the claim supported?"}]


def test_judge_config_sends_only_the_parameters_that_are_set():
    assert JudgeConfig().request_params() == {"model": DEFAULT_JUDGE_MODEL}
    config = JudgeConfig(model="gpt-4.1", temperature=0.0)
    assert config.request_params() == {"model": "gpt-4.1", "temperature": 0.0}
    assert config.describe() == {"model": "gpt-4.1", "reasoning_effort": None, "temperature": 0.0}


def test_every_request_goes_to_the_judge_model_with_the_judge_parameters(monkeypatch):
    verdict = Verdict(reasoning="ok", result=True)
    completions = ScriptedCompletions(completion(parsed=verdict))
    client = make_client(completions, monkeypatch, judge=JudgeConfig(reasoning_effort="high"))
    result, tokens = asyncio.run(client.async_response(
        model="o4-mini", temperature=0.0, messages=MESSAGES, response_format=Verdict, count_token=True))
    assert result is verdict
    assert tokens == {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 5, "reasoning_tokens": 3}
    method, sent = completions.calls[0]
    assert method == "parse"
    assert sent == {"model": DEFAULT_JUDGE_MODEL, "reasoning_effort": "high",
                    "messages": MESSAGES, "response_format": Verdict}


def test_without_a_judge_requests_are_sent_as_given(monkeypatch):
    completions = ScriptedCompletions(completion(content="hello"))
    client = make_client(completions, monkeypatch)
    assert asyncio.run(client.async_response(model="gpt-4.1", temperature=0.2, messages=MESSAGES)) == "hello"
    assert completions.calls == [("create", {"model": "gpt-4.1", "temperature": 0.2, "messages": MESSAGES})]


def test_transient_errors_are_retried(monkeypatch):
    verdict = Verdict(reasoning="ok", result=False)
    completions = ScriptedCompletions(
        status_error(openai.RateLimitError, 429, headers={"retry-after-ms": "1"}),
        openai.APIConnectionError(request=REQUEST),
        status_error(openai.InternalServerError, 503),
        completion(parsed=verdict),
    )
    client = make_client(completions, monkeypatch, judge=JudgeConfig())
    assert asyncio.run(client.async_response(messages=MESSAGES, response_format=Verdict)) is verdict
    assert len(completions.calls) == 4


@pytest.mark.parametrize("error", [
    status_error(openai.BadRequestError, 400),
    status_error(openai.AuthenticationError, 401),
    status_error(openai.RateLimitError, 429, code="insufficient_quota"),
])
def test_permanent_errors_raise_judge_error_without_retrying(monkeypatch, error):
    completions = ScriptedCompletions(error)
    client = make_client(completions, monkeypatch, judge=JudgeConfig())
    with pytest.raises(JudgeError, match=type(error).__name__):
        asyncio.run(client.async_response(messages=MESSAGES, response_format=Verdict))
    assert len(completions.calls) == 1


def test_transient_errors_beyond_the_retry_budget_raise_judge_error(monkeypatch):
    completions = ScriptedCompletions(status_error(openai.InternalServerError, 500))
    client = make_client(completions, monkeypatch, judge=JudgeConfig(), retry_seconds=0)
    with pytest.raises(JudgeError, match="InternalServerError"):
        asyncio.run(client.async_response(messages=MESSAGES, response_format=Verdict))


def test_missing_structured_output_raises_judge_error(monkeypatch):
    completions = ScriptedCompletions(completion(parsed=None, refusal="I can't help with that."))
    client = make_client(completions, monkeypatch, judge=JudgeConfig())
    with pytest.raises(JudgeError, match="refusal: I can't help with that"):
        asyncio.run(client.async_response(messages=MESSAGES, response_format=Verdict))


def test_synchronous_client_behaves_like_the_async_one(monkeypatch):
    completions = ScriptedCompletions(status_error(openai.RateLimitError, 429), completion(content="pong"),
                                      is_async=False)
    client = make_client(completions, monkeypatch, judge=JudgeConfig(model="gpt-4.1", temperature=0.0))
    assert client.response(messages=MESSAGES) == "pong"
    assert completions.calls[-1] == ("create", {"messages": MESSAGES, "model": "gpt-4.1", "temperature": 0.0})
    with pytest.raises(ValueError, match="synchronous"):
        asyncio.run(client.async_response(messages=MESSAGES))


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="not supported"):
        LLMClient("bedrock_anthropic")
