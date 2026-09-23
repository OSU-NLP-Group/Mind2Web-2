"""The judge: which model scores answers, how its requests are parameterized, and how they are accounted.

One judge scores every answer of an evaluation run.  :class:`JudgeConfig` names
the model and its request parameters; :class:`~mind2web2.llm_client.LLMClient`
sends every request to that model, whatever model an eval script names, so a
run never mixes judges.  :class:`JudgeUsage` accumulates the requests made for
one answer, and :class:`JudgeError` marks a request that failed for good.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_JUDGE_MODEL = "gpt-6-luna"


@dataclass(frozen=True)
class JudgeConfig:
    """The judge model and the request parameters it is called with.

    ``reasoning_effort`` and ``temperature`` are sent only when set; ``None``
    keeps the model's default.  Reasoning models such as ``gpt-6-luna`` and
    ``o4-mini`` accept ``reasoning_effort`` and reject ``temperature``;
    non-reasoning models such as ``gpt-4.1`` are the other way around.
    """

    model: str = DEFAULT_JUDGE_MODEL
    reasoning_effort: str | None = None
    temperature: float | None = None

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

    Raised after transient failures (rate limits, timeouts, connection errors,
    server errors) have been retried until the client's retry budget ran out,
    and immediately for any other failure: invalid request, authentication,
    exhausted quota, truncated or refused output.  Evaluation code lets it
    propagate instead of scoring the verification as failed, so that a judge
    outage never lowers an agent's score.
    """


@dataclass
class JudgeUsage:
    """Judge requests made while evaluating one answer, with their token usage."""

    requests: int = 0
    failed_requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def record(self, tokens: dict[str, int]) -> None:
        """Add one successful request with the token counts returned by the client."""
        self.requests += 1
        self.input_tokens += tokens.get("input_tokens", 0)
        self.cached_input_tokens += tokens.get("cached_input_tokens", 0)
        self.output_tokens += tokens.get("output_tokens", 0)
        self.reasoning_tokens += tokens.get("reasoning_tokens", 0)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)
