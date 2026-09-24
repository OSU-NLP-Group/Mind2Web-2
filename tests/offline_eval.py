"""Run a task evaluation script offline, with a fake judge and a synthetic webpage cache.

An eval script's ``evaluate_answer`` coroutine normally talks to an LLM judge and
reads cached webpages.  This module replaces both with deterministic stand-ins so
that every script can be executed end to end without network access, API keys,
or a browser.  The output is the script's final score plus a canonical form of
its rubric tree, which makes behavior changes in the harness visible as diffs.

Policies control what the fake judge returns:

``all_true``
    Every verification passes; list-typed extraction fields get 2 items.
``hash``
    A verification passes unless the SHA-256 of its request messages is divisible
    by 3, which yields a stable mix of passes and failures; lists get 2 items.
``empty``
    Every list-typed extraction field is empty; verifications pass.  Exercises
    the "answer provided nothing" paths, e.g. padding loops.
``long``
    Every list-typed extraction field gets 12 items; verifications pass.
    Exercises truncation to the number of items a task requires.

Every run happens at a fixed time, :data:`FROZEN_NOW` (2026-09-22 12:00 UTC,
the date the golden files were recorded), with UTC as the local time zone, so
a script that reads the current date or year produces the same tree on any
day and on any machine.  Anything a script prints goes to stderr, so that
stdout carries only the JSON result.

Run one script (prints a JSON result to stdout)::

    uv run python tests/offline_eval.py --script eval_scripts/dev_set/yu_lineage.py --policy hash
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import enum
import hashlib
import io
import json
import logging
import sys
import traceback
import types
import typing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import time_machine
from PIL import Image
from pydantic import BaseModel

POLICIES = ("all_true", "hash", "empty", "long")
LIST_LENGTHS = {"all_true": 2, "hash": 2, "empty": 0, "long": 12}

#: The clock during a script run (see the module docstring).
FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=ZoneInfo("UTC"))

DEFAULT_ANSWER = (
    "Offline answer used when no answer file is available.\n\n"
    "Sources: https://example.com/a and https://example.org/b"
)

_URL_HINTS = ("url", "link", "source", "href", "website", "page")
_YEAR_HINTS = ("year",)
_DATE_HINTS = ("date", "day")
_NUMBER_HINTS = ("price", "cost", "amount", "number", "count", "rating", "score", "distance",
                 "salary", "fee", "total", "population", "height", "weight", "age", "rank")


def _tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="JPEG")
    return buf.getvalue()


_TINY_JPEG = _tiny_jpeg()


# --------------------------------------------------------------------------- #
# Synthetic structured outputs                                                #
# --------------------------------------------------------------------------- #

def _string_for(field_name: str, index: int) -> str:
    """Return a plausible string for a field, guided by the field's name."""
    name = field_name.lower()
    if any(h in name for h in _URL_HINTS):
        return f"https://example.com/offline/{name}/{index}"
    if any(h in name for h in _YEAR_HINTS):
        return "2024"
    if any(h in name for h in _DATE_HINTS):
        return "2024-01-15"
    if any(h in name for h in _NUMBER_HINTS):
        return "100"
    return f"offline {name} {index}"


def _value_for(annotation: Any, field_name: str, depth: int, index: int, list_len: int) -> Any:
    if depth > 6:
        return None
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        return _value_for(non_none[0], field_name, depth, index, list_len) if non_none else None
    if origin is typing.Literal:
        return args[0]
    if origin in (list, set, tuple):
        inner = args[0] if args else str
        return [_value_for(inner, field_name, depth + 1, i, list_len) for i in range(list_len)]
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return synthesize(annotation, list_len, depth + 1, index)
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation))
        if annotation is bool:
            return True
        if annotation is int:
            return 1
        if annotation is float:
            return 1.0
    return _string_for(field_name, index)


def synthesize(model: type[BaseModel], list_len: int, depth: int = 0, index: int = 0) -> BaseModel:
    """Build an instance of ``model`` with deterministic, type-appropriate values.

    ``index`` is the position of this instance within an enclosing list; string
    fields embed it so that list items are distinct (scripts often deduplicate).
    """
    values = {
        name: _value_for(field.annotation, name, depth, index, list_len)
        for name, field in model.model_fields.items()
    }
    try:
        return model.model_validate(values)
    except Exception:  # validators in eval scripts may reject synthetic values
        return model.model_construct(**values)


# --------------------------------------------------------------------------- #
# Fakes passed to evaluate_answer                                             #
# --------------------------------------------------------------------------- #

class FakeLLMClient:
    """Stands in for ``LLMClient(..., is_async=True)``; answers from the policy.

    Records the model name of every request in ``models_requested``.  Like the
    real client, it returns ``(result, tokens)`` when called with ``count_token=True``.
    """

    def __init__(self, policy: str):
        self.policy = policy
        self.list_len = LIST_LENGTHS[policy]
        self.calls = 0
        self.models_requested: set[str] = set()

    def _verdict(self, text: str) -> bool:
        if self.policy != "hash":
            return True
        return int(hashlib.sha256(text.encode()).hexdigest(), 16) % 3 != 0

    async def async_response(self, count_token: bool = False, **kwargs: Any) -> Any:
        self.calls += 1
        self.models_requested.add(str(kwargs.get("model")))
        result = self._answer(kwargs)
        return (result, {"input_tokens": 1, "output_tokens": 1}) if count_token else result

    def _answer(self, kwargs: dict[str, Any]) -> Any:
        response_format = kwargs.get("response_format")
        request_text = json.dumps(kwargs.get("messages", []), sort_keys=True, default=str)
        if response_format is None:
            return "offline text response"
        fields = response_format.model_fields
        if "result" in fields and "reasoning" in fields and fields["result"].annotation is bool:
            return response_format(reasoning="offline", result=self._verdict(request_text))
        instance = synthesize(response_format, self.list_len)
        for name, field in fields.items():
            if field.annotation is bool:
                object.__setattr__(instance, name, self._verdict(request_text + name))
        return instance

    def response(self, **kwargs: Any) -> Any:
        raise RuntimeError("Eval scripts are expected to use the async client")


class SyntheticCache:
    """Duck-typed ``CacheFileSys`` that serves a synthetic page for every URL."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def has(self, url: str) -> str:
        self.requested.append(url)
        return "web"

    def has_web(self, url: str) -> bool:
        return True

    def has_pdf(self, url: str) -> bool:
        return False

    def get_web(self, url: str, get_screenshot: bool = True):
        return f"Offline cached page for {url}.", (_TINY_JPEG if get_screenshot else None)

    def get_pdf(self, url: str) -> bytes:
        raise KeyError(url)

    def put_web(self, *args: Any, **kwargs: Any) -> None:
        pass

    def put_pdf(self, *args: Any, **kwargs: Any) -> None:
        pass

    def save(self) -> None:
        pass

    def get_all_urls(self) -> list[str]:
        return []


class FakeGoogleMapsTool:
    """Stands in for ``mind2web2.api_tools.tool_googlemap.GoogleMapsTool``."""

    async def get_city_name(self, address, level="locality"):
        return "Columbus"

    async def get_address_information(self, address):
        return [{"formatted_address": str(address), "address_components": [],
                 "geometry": {"location": {"lat": 40.0, "lng": -83.0}}}]

    async def calculate_distance(self, address1, address2, mode="driving"):
        return 1000

    async def calculate_travel_time(self, address1, address2, mode="driving"):
        return 600


# --------------------------------------------------------------------------- #
# Running a script                                                            #
# --------------------------------------------------------------------------- #

def canonical_tree(node: dict) -> dict:
    """Keep only the fields of a serialized VerificationNode that define the rubric result."""
    return {
        "id": node.get("id"),
        "critical": node.get("critical"),
        "strategy": node.get("strategy"),
        "score": round(float(node.get("score", 0.0)), 6),
        "status": node.get("status"),
        "children": [canonical_tree(c) for c in node.get("children", [])],
    }


def render_result(final_score: float, tree: dict) -> str:
    """Render a canonical result as text, one rubric node per line, indented by depth.

    Each line reads ``<id> [<strategy>[, critical]] <score> <status>``, so a diff
    between two renderings points at the exact nodes whose outcome changed.
    """
    lines = [f"final_score {final_score:.6g}"]

    def visit(node: dict, depth: int) -> None:
        flags = node["strategy"] + (", critical" if node["critical"] else "")
        lines.append(f"{'  ' * depth}{node['id']} [{flags}] {node['score']:.6g} {node['status']}")
        for child in node["children"]:
            visit(child, depth + 1)

    visit(tree, 0)
    return "\n".join(lines) + "\n"


async def run_script(script: Path, policy: str, answer: str, model: str = "o4-mini") -> dict:
    """Run one eval script with fakes, at :data:`FROZEN_NOW`, and return its canonical result.

    Returns ``{"ok": True, "final_score", "tree", "llm_calls", "models_requested"}``
    on success and ``{"ok": False, "error", "traceback"}`` when the script raises.
    """
    import mind2web2
    from mind2web2 import api_tools
    from mind2web2.api_tools import tool_googlemap
    from mind2web2.eval_runner import DualSemaphore
    from mind2web2.utils.load_eval_script import load_eval_script

    # Scripts import the tool at load time, from any of the modules that export it
    for module in (tool_googlemap, api_tools, mind2web2):
        module.GoogleMapsTool = FakeGoogleMapsTool

    logger = logging.getLogger(f"offline.{script.stem}.{policy}")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    client = FakeLLMClient(policy)
    try:
        with time_machine.travel(FROZEN_NOW, tick=False):  # from loading: scripts may read the clock at import
            evaluate_answer = load_eval_script(str(script))
            result = await evaluate_answer(
                client=client, answer=answer, agent_name="offline", answer_name="answer_1.md",
                cache=SyntheticCache(), logger=logger, model=model,
                semaphore=DualSemaphore(asyncio.Semaphore(10), asyncio.Semaphore(30)),
            )
        tree = result["eval_breakdown"][0]["verification_tree"]
        return {
            "ok": True,
            "final_score": round(float(result["final_score"]), 6),
            "tree": canonical_tree(tree),
            "llm_calls": client.calls,
            "models_requested": sorted(client.models_requested),
        }
    except Exception as exc:  # report, do not raise: the caller compares outcomes
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
            "traceback": traceback.format_exc().splitlines()[-8:],
        }


def answer_for(script: Path, answers_dir: Path | None) -> str:
    """Return ``<answers_dir>/<task_id>/answer_1.md`` if it exists, else a default answer."""
    if answers_dir is not None:
        candidate = answers_dir / script.stem / "answer_1.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return DEFAULT_ANSWER


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--policy", choices=POLICIES, required=True)
    parser.add_argument("--answers-dir", type=Path, default=None,
                        help="Directory laid out as <task_id>/answer_1.md")
    parser.add_argument("--model", default="o4-mini")
    args = parser.parse_args()
    answer = answer_for(args.script, args.answers_dir)
    with contextlib.redirect_stdout(sys.stderr):  # keep stdout for the result
        result = asyncio.run(run_script(args.script, args.policy, answer, args.model))
    json.dump(result, sys.stdout, sort_keys=True)


if __name__ == "__main__":
    main()
