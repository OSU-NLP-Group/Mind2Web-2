# llm_client — The Judge's LLM Client

Chat Completions client used by the judge (and by the webpage-caching script for URL extraction).

## judge.py — Judge Configuration and Accounting
- `DEFAULT_JUDGE_MODEL = "gpt-6-luna"`: the judge used when none is given.
- `JudgeConfig(model, reasoning_effort=None, temperature=None)`: the judge model and its request parameters. `reasoning_effort` and `temperature` are sent only when set (reasoning models reject `temperature`; non-reasoning models reject `reasoning_effort`). `describe()` is recorded in every evaluation result.
- `JudgeError`: a judge request failed for good (non-retryable error, or transient errors beyond the retry budget, or no structured output). Evaluation code must let it propagate: the answer is then reported as not scored and evaluated again on the next run. A failure that recurs on every run (a refusal, a rejected request) keeps the answer without a result, which the metrics count as 0. After one failed request, the answer's later requests fail without being sent.
- `JudgeUsage`: requests, failed requests, and token counts (input, cached input, output, reasoning) for one answer; recorded in the result as `judge_usage`.

## base_client.py — `LLMClient`
- `LLMClient(provider="openai" | "azure_openai", is_async, judge=None, base_url=None, retry_seconds=900, ...)`.
- OpenAI provider: `OPENAI_API_KEY`, and `base_url` or `OPENAI_BASE_URL` for any OpenAI-compatible server (e.g. a LiteLLM proxy). Azure provider: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT_URL`, `AZURE_OPENAI_API_VERSION`.
- With `judge` set, every request is sent to `judge.model` with the judge's parameters, replacing whatever `model` / `temperature` / `reasoning_effort` the caller passed (logged once per replaced model). This keeps one judge per run even when an eval script names a model.
- `response(**kwargs)` / `async_response(**kwargs)`: a Pydantic `response_format` goes to `chat.completions.parse` and returns the parsed object; otherwise `chat.completions.create` returns the message text. `count_token=True` returns `(result, tokens)`.
- Retries: connection errors, timeouts, and HTTP 408, 409, 429, and 5xx responses (unless the response's `x-should-retry` header says otherwise; the rules the OpenAI SDK uses) are retried with jittered exponential backoff (honoring `retry-after`) until `retry_seconds` have passed; each retry's timeout is cut to the time left, so a request takes about `max(timeout, retry_seconds)` at most (the SDK applies the timeout per phase of an attempt). Exhausted quota and every other error raise `JudgeError` immediately. After one request has spent its whole budget unable to connect (connection refused or host name not resolved; a connection timeout counts as a timeout), later requests that cannot connect fail after one attempt until a request succeeds. The SDK's own retries are disabled.

## api_cost.py
`calculate_api_cost(input_tokens, output_tokens, model_name)`: per-million-token price lookup for a few models.

## Usage
```python
judge = JudgeConfig(model="gpt-6-luna")
client = LLMClient(provider="openai", is_async=True, judge=judge)
verdict, tokens = await client.async_response(
    messages=[...], response_format=MyPydanticModel, count_token=True,
)
```
