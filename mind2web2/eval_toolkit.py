from __future__ import annotations

import asyncio
import base64
import functools
import io
import logging
import random
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Iterator, List, Type, Callable, Awaitable, Optional, Sequence, Tuple, Union

from PIL import Image
from pydantic import BaseModel, ValidationError

from .api_tools import tool_pdf
from .llm_client.base_client import LLMClient
from .llm_client.judge import (
    DEFAULT_JUDGE_MODEL, ContextLengthError, JudgeContentError, JudgeError, JudgeUsage,
)
from .utils.cache_filesys import CacheFileSys, CacheIndexError
from .utils.misc import (
    text_dedent, normalize_url_markdown
)
from .utils.page_info_retrieval import (
    BatchBrowserManager,
)
from .api_tools.tool_pdf import is_pdf
from .verification_tree import VerificationNode


def empty_extraction(template_class: Type[BaseModel]) -> BaseModel:
    """What an extraction returns when there is nothing to extract from.

    That is the case when the page is unavailable, when the judge rejected
    the extraction request because of its content (:class:`JudgeContentError`),
    and when the request failed for a reason other than a :class:`JudgeError`.
    The result is
    ``template_class()`` when its fields all have defaults; otherwise it is an
    unvalidated instance with the defaults and ``None`` for each required
    field, so that a script reading it gets empty values instead of an
    exception that would leave the whole answer unscored.
    """
    try:
        return template_class()
    except ValidationError:
        required = {name: None for name, field in template_class.model_fields.items() if field.is_required()}
        return template_class.model_construct(**required)


class BinaryEvalResult(BaseModel):
    reasoning: str
    result: bool


#: The tokenizer that page-text budgets are counted in: the encoding of OpenAI's current models.
TEXT_ENCODING = "o200k_base"
TRUNCATION_MARKER = "\n… [CONTENT TRUNCATED]"


class HarnessError(JudgeError):
    """The evaluation environment failed while loading a page, so the answer cannot be scored now.

    Raised when the tokenizer that page text is counted in cannot be loaded
    (``tiktoken`` downloads it on first use), when the browser fails outside a
    page load, for example because it cannot be launched, or when the task's
    page cache cannot be read or written (a disk error, an index file that
    cannot be parsed, or a page file that the index lists but that is
    missing, as when another process changes the cache during the run).
    Such a failure says nothing about the answer, so it must not fail the
    check that loaded the page.  It subclasses :class:`JudgeError` so that every handler that
    lets a judge failure propagate does the same for it: the answer is
    reported as not scored and evaluated again on the next run.
    """


@functools.lru_cache(maxsize=1)
def _text_encoding():
    """The :data:`TEXT_ENCODING` tokenizer; raises :class:`HarnessError` if it cannot be loaded."""
    try:
        import tiktoken  # loads (and on first use downloads) the encoding, so only when a text needs counting
        return tiktoken.get_encoding(TEXT_ENCODING)
    except Exception as exc:
        raise HarnessError(f"The {TEXT_ENCODING} tokenizer cannot be loaded: {exc}") from exc


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """``text`` cut off after ``max_tokens`` tokens, with :data:`TRUNCATION_MARKER` appended; unchanged if it is not longer.

    Tokens are counted in :data:`TEXT_ENCODING`.  Another judge's tokenizer
    counts somewhat differently; the budget is set far enough below the judge's
    context length to absorb that.  Every token covers at least one byte of
    UTF-8, so a text of at most ``max_tokens`` bytes is returned without being
    tokenized.  The cut keeps the text's whitespace and line breaks.
    """
    if len(text.encode("utf-8")) <= max_tokens:
        return text
    encoding = _text_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[:max_tokens]).rstrip("\ufffd") + TRUNCATION_MARKER


def count_tokens(text: str) -> int:
    """The number of tokens in ``text``, counted in :data:`TEXT_ENCODING`."""
    return len(_text_encoding().encode(text, disallowed_special=()))


def split_screenshot(image: Image.Image, part_height: int, overlap: int, max_parts: int) -> List[Image.Image]:
    """``image`` as parts of at most ``part_height`` pixels, top to bottom, each overlapping the previous one by ``overlap`` pixels.

    An image no taller than ``part_height`` is returned as its only part,
    unchanged.  At most ``max_parts`` parts are made, so an image taller than
    ``part_height + (max_parts - 1) * (part_height - overlap)`` pixels loses
    its bottom.
    """
    if image.height <= part_height:
        return [image]
    parts: List[Image.Image] = []
    top = 0
    while len(parts) < max_parts:
        bottom = min(top + part_height, image.height)
        parts.append(image.crop((0, top, image.width, bottom)))
        if bottom == image.height:
            break
        top = bottom - overlap
    return parts


class Screenshots(list):
    """The base64 JPEG images of one page that go to the judge, in order.

    ``split`` is true when a screenshot was split into parts
    (:func:`split_screenshot`), so that the judge request says how the parts
    fit together.
    """

    split: bool = False


_shared_browser: ContextVar[Optional[BatchBrowserManager]] = ContextVar("mind2web2_shared_browser", default=None)


@contextmanager
def shared_browser(manager: BatchBrowserManager) -> Iterator[BatchBrowserManager]:
    """Make every evaluator created inside the ``with`` block capture live pages with ``manager``.

    Eval scripts create their evaluators themselves and pass no browser.  A
    runner that evaluates many answers wraps them in this block so that all of
    them share one browser, which opens at most ``manager.max_concurrent_pages``
    pages in total; the runner stops it at the end.  An evaluator created
    outside such a block creates its own browser, which starts at its first live
    capture and is stopped by :meth:`mind2web2.evaluator.Evaluator.close`.  The
    setting is a context variable, so it also reaches asyncio tasks created
    inside the block.
    """
    token = _shared_browser.set(manager)
    try:
        yield manager
    finally:
        _shared_browser.reset(token)


def _new_browser() -> BatchBrowserManager:
    """The browser an evaluator creates for itself when no browser is given or shared."""
    return BatchBrowserManager(headless=False, max_concurrent_pages=50, max_retries=1)


@asynccontextmanager
async def browser_for_run(max_concurrent_pages: int = 5) -> AsyncIterator[None]:
    """Share one browser among the evaluators created inside this block, unless one is already shared.

    Outside :func:`shared_browser`, a browser with at most
    ``max_concurrent_pages`` pages open is shared for the block and stopped at
    its end; it is launched only if a page has to be captured live.  Inside
    :func:`shared_browser`, the block uses that browser and stops nothing.
    """
    if _shared_browser.get() is not None:
        yield
        return
    manager = BatchBrowserManager(headless=False, max_concurrent_pages=max_concurrent_pages, max_retries=1)
    try:
        with shared_browser(manager):
            yield
    finally:
        await manager.stop()


class EvaluatorConfig:
    """Evaluator configuration settings.

    The page limits keep each judge request within what the judge model reads
    in full.  Page text is cut off after ``max_text_tokens`` tokens, far below
    the context length of current judges and below the 272K input tokens above
    which ``gpt-6-luna`` bills a request at twice the rate.  A screenshot is
    scaled down to ``image_max_width`` pixels wide and split into parts of at
    most ``image_part_height`` pixels, each overlapping the previous one by
    ``image_part_overlap`` pixels; a 1100 x 2000 part is within the size that
    OpenAI's models accept at ``detail: "high"`` without scaling it down
    further, while a whole long screenshot would be scaled down until its text
    is hard to read.  At most ``image_max_parts`` parts are sent per
    screenshot, so the judge sees the top 9,600 pixels of a long page.
    """
    max_text_tokens: int = 100_000  # tokens of page text, counted in TEXT_ENCODING
    max_text_shrinks: int = 2       # times the page text is halved when a request exceeds the judge's context length
    image_max_width: int = 1100     # pixels; a wider screenshot is scaled down to this width
    image_part_height: int = 2000   # pixels, after that scaling; a taller screenshot is split into parts
    image_part_overlap: int = 100   # pixels shared by consecutive parts
    image_max_parts: int = 5        # parts per screenshot; the rest of a taller screenshot is left out
    jpeg_quality: int = 85
    default_num_trials: int = 3
    default_majority_vote: bool = True
    default_use_screenshot: bool = True
    default_additional_instruction: str = "None"

    def as_dict(self) -> dict:
        """The settings by name, as recorded in each evaluation result."""
        return {name: getattr(self, name) for name in type(self).__annotations__}


class BaseEvaluator:
    """Common utilities shared by Extractor & Verifier."""

    def __init__(
            self,
            *,
            client: LLMClient,
            task_description: str,
            answer: str,
            global_cache: CacheFileSys,
            global_semaphore: asyncio.Semaphore,
            logger: logging.Logger,
            model: str = DEFAULT_JUDGE_MODEL,
            config: Optional[EvaluatorConfig] = None,
            browser_manager: Optional[BatchBrowserManager] = None,
            usage: Optional[JudgeUsage] = None,
    ) -> None:
        self.client = client
        self.task_description = task_description
        self.answer = answer
        self.cache = global_cache
        self.semaphore = global_semaphore
        self.logger = logger
        self.pdf_parser = tool_pdf.PDFParser()
        self.MODEL_NAME = model
        self.usage = usage if usage is not None else JudgeUsage()
        self.config = config or EvaluatorConfig()
        browser_manager = browser_manager or _shared_browser.get()
        #: Whether this evaluator created its browser, and so is the one to stop it.
        self.owns_browser = browser_manager is None
        self.browser_manager = browser_manager or _new_browser()

    async def call_llm_with_semaphore(self, **kwargs):
        """Send one judge request under the LLM semaphore and record it in ``self.usage``.

        Raises :class:`JudgeError` when the request fails for good; callers let it
        propagate so that the answer is reported as not scored.  Once a request
        for the answer has failed for good, the answer cannot be scored, so
        later requests raise :class:`JudgeError` at once without being sent;
        its message names the first failure, so that whichever of the errors
        reaches the log (concurrent checks can finish in any order) names the
        cause.  A :class:`JudgeContentError` (the judge rejected this request's
        content) is raised without counting as a failed request: callers score
        the check that made the request as failed and record the rejection.
        """
        # Use LLM semaphore if available, fallback to default semaphore
        semaphore_to_use = getattr(self.semaphore, 'llm', self.semaphore)
        async with semaphore_to_use:
            if self.usage.failed_requests:
                raise JudgeError(f"An earlier judge request for this answer failed for good "
                                 f"({self.usage.first_failure}); no further requests are sent for it")
            try:
                result, tokens = await self.client.async_response(count_token=True, **kwargs)
            except JudgeContentError:
                raise
            except JudgeError as exc:
                self.usage.failed_requests += 1
                if self.usage.first_failure is None:
                    self.usage.first_failure = f"{type(exc).__name__}: {exc}"
                raise
        self.usage.record(tokens)
        return result

    def _record_rejection(self, context: dict, error: JudgeContentError) -> None:
        """Log and record a request that the judge rejected because of its content.

        ``context`` describes the extraction or check that sent it; the rejection
        is recorded under its ``op_id`` and ``url``.
        """
        self.logger.warning(f"The judge rejected a request of {_subject(context)}; it counts as failed: {error}",
                            extra={"op_id": context["op_id"]})
        self.usage.record_rejection(context["op_id"], context.get("url"), error)

    async def _with_shorter_text_on_overflow(
            self,
            web_text: str,
            attempt: Callable[[str], Awaitable],
            context: dict,
    ):
        """``await attempt(web_text)``, cutting the page text in half and trying again while the request is too long.

        When the judge answers :class:`ContextLengthError`, the page text is cut
        to half the smaller of its token count and its token budget, up to
        ``config.max_text_shrinks`` times, so each attempt sends at most half
        the page text of the one before; the last :class:`ContextLengthError`
        propagates.  ``context`` describes the extraction or check, for the log.
        """
        budget = self.config.max_text_tokens
        for shrink in range(self.config.max_text_shrinks + 1):
            try:
                return await attempt(web_text)
            except ContextLengthError:
                if shrink == self.config.max_text_shrinks:
                    raise
                try:
                    budget = min(budget, await asyncio.to_thread(count_tokens, web_text)) // 2
                    self.logger.warning(f"A request of {_subject(context)} is too long for the judge; "
                                        f"sending it again with the page text cut to {budget} tokens",
                                        extra={"op_id": context["op_id"]})
                    web_text = await asyncio.to_thread(truncate_to_tokens, web_text, budget)
                except HarnessError as exc:
                    self._count_harness_failure("Shortening a page text", exc)
                    raise

    def _build_message_content(self, prompt: str, screenshot_b64: List[str], use_screenshot: bool = True):
        """Build message content"""
        if use_screenshot and screenshot_b64:
            intro = "\n\nBelow are rendered page screenshots to provide non-textual context:"
            if getattr(screenshot_b64, "split", False):
                intro = ("\n\nBelow are rendered page screenshots to provide non-textual context. A screenshot "
                         f"taller than {self.config.image_part_height} pixels is split into consecutive parts "
                         f"from top to bottom, each overlapping the previous part by "
                         f"{self.config.image_part_overlap} pixels:")
            msg_content = [{"type": "text", "text": prompt + intro}]
            image_content = [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
                }
                for b64 in screenshot_b64
            ]
            msg_content.extend(image_content)  # type: ignore
            return msg_content
        else:
            return [{"type": "text", "text": prompt}]

    async def _capture_and_cache(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Capture ``url`` in the browser under the webpage semaphore.

        A captured page is stored in the cache and returned as
        ``(screenshot_b64, text)``.  A failed capture is recorded in the cache
        and returns ``(None, None)``.
        """
        webpage_semaphore = getattr(self.semaphore, 'webpage', self.semaphore)
        async with webpage_semaphore:
            await asyncio.sleep(0.2 * random.random())
            try:
                capture = await self.browser_manager.capture(url, self.logger)
            except Exception as exc:  # a failed page load is returned in the Capture; this is the browser itself
                raise HarnessError(f"The browser failed while capturing {url}: {exc}") from exc
        if not capture.ok:
            self.logger.warning(f"Could not capture {url}: {capture.error}")
            await self._cache(self.cache.record_failure, url, capture.error, blocked=capture.blocked)
            return None, None
        await self._cache(self.cache.put_web, url, capture.text, capture.screenshot_b64)
        return capture.screenshot_b64, capture.text

    async def _fetch_live(self, url: str) -> Tuple[Optional[Union[str, List[str]]], Optional[str]]:
        """Fetch a page that is not cached, store it, and return its screenshots and text.

        A URL that serves a PDF is downloaded; any other URL, including a
        PDF-looking URL whose response is not a PDF, is loaded in the browser.
        The PDF check and download run under the webpage semaphore, like
        browser captures.
        """
        webpage_semaphore = getattr(self.semaphore, 'webpage', self.semaphore)
        pdf_bytes = None
        async with webpage_semaphore:
            if await is_pdf(url):
                pdf_bytes = await self.pdf_parser.fetch(url)
                if pdf_bytes is None:
                    self.logger.debug(f"{url} did not return a PDF; loading it in the browser")
        if pdf_bytes is not None:
            await self._cache(self.cache.put_pdf, url, pdf_bytes)
            return await self.pdf_parser.extract(pdf_bytes)
        return await self._capture_and_cache(url)

    async def get_page_info(self, url: str, cancellation_event: Optional[asyncio.Event] = None):
        """The page at ``url`` as ``(screenshots_b64, text)``, or ``(None, None)`` if it is unavailable.

        Cached pages are served from the task's cache.  A URL with a failure
        record in the cache (its capture already failed, during crawling or
        an earlier evaluation) is unavailable, and it is not captured again,
        so that its result does not depend on when the evaluation runs.  Any
        other URL is fetched live and stored (see :meth:`_fetch_live`).

        The screenshots come back as :class:`Screenshots`, JPEGs of at most
        ``config.image_max_width`` by ``config.image_part_height`` pixels (1100
        by 2000 by default): a wider screenshot is scaled down to that width,
        and a taller one is then split into overlapping parts, at most
        ``config.image_max_parts`` of them (:func:`split_screenshot`), so the
        judge sees the top 9,600 pixels of a long page at full scale.  A
        screenshot that cannot be processed this way, such as a corrupt file,
        is left out rather than sent unchecked.  Text longer than
        ``config.max_text_tokens`` tokens is cut off (:func:`truncate_to_tokens`).
        A URL that cannot be parsed is unavailable.

        Raises :class:`HarnessError` when the tokenizer, the browser, or the
        page cache fails, logs it and counts it in ``usage.harness_failures``;
        once one page load of the answer has failed this way, later ones raise
        it at once.
        """
        if self.usage.harness_failures:
            raise HarnessError("An earlier page load for this answer failed because of the evaluation "
                               "environment; no further pages are loaded for it")
        try:
            return await self._load_page(url, cancellation_event)
        except HarnessError as exc:
            self._count_harness_failure(f"Loading {url}", exc)
            raise

    def _count_harness_failure(self, action: str, exc: HarnessError) -> None:
        """Count ``exc`` in ``usage.harness_failures`` and log that ``action`` failed because of the environment."""
        self.usage.harness_failures += 1
        self.logger.error(f"{action} failed because of the evaluation environment; "
                          f"the answer will not be scored: {exc}")

    async def _cache(self, method: Callable, *args, in_thread: bool = True, **kwargs):
        """Call a method of the task's page cache, in a worker thread unless ``in_thread`` is false.

        A disk error, an index file that cannot be parsed, or a page file that
        the index lists but that is missing (``OSError``, including
        ``FileNotFoundError``, and :class:`CacheIndexError`) is raised as
        :class:`HarnessError`: it says nothing about the page, so it must not
        fail a check, and evaluating again after the cache is repaired scores
        the answer.  Other errors,
        such as ``KeyError`` or ``ValueError``, propagate unchanged.
        """
        try:
            if in_thread:
                return await asyncio.to_thread(method, *args, **kwargs)
            return method(*args, **kwargs)
        except (OSError, CacheIndexError) as exc:
            raise HarnessError(f"The page cache failed in {method.__name__}: {exc}") from exc

    async def _load_page(self, url: str, cancellation_event: Optional[asyncio.Event]):
        """The body of :meth:`get_page_info`, without the accounting of :class:`HarnessError`."""
        url = normalize_url_markdown(url)
        self.logger.debug(f"Loading {url}")
        if cancellation_event and cancellation_event.is_set():
            self.logger.debug(f"Page info retrieval cancelled for {url}")
            return None, None

        try:
            content_type = await self._cache(self.cache.has, url, in_thread=False)
        except ValueError as exc:
            self.logger.warning(f"{url} is unavailable: it cannot be parsed ({exc})")
            return None, None
        if content_type == "pdf":
            pdf_bytes = await self._cache(self.cache.get_pdf, url)
            screenshot_b64, page_text = await self.pdf_parser.extract(pdf_bytes)
        elif content_type == "web":
            page_text, screenshot_bytes = await self._cache(self.cache.get_web, url)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        elif (failure := await self._cache(self.cache.failure, url, in_thread=False)) is not None:
            self.logger.warning(f"{url} is unavailable: its capture failed ({failure['reason']})")
            return None, None
        else:
            self.logger.warning(f"{url} is not in the cache; capturing it live")
            screenshot_b64, page_text = await self._fetch_live(url)

        if page_text is None:
            self.logger.warning(f"{url} is unavailable: no content could be retrieved")
            return None, None

        images = screenshot_b64 if isinstance(screenshot_b64, list) else [screenshot_b64]

        def _encode(image: Image.Image) -> str:
            buf = io.BytesIO()
            image.save(buf, format="JPEG", optimize=True, quality=self.config.jpeg_quality)
            return base64.b64encode(buf.getvalue()).decode()

        def _parts(b64_str: str) -> Optional[List[str]]:
            try:
                with Image.open(io.BytesIO(base64.b64decode(b64_str))) as im:
                    # Pillow PNG/GIF needs to be converted to RGB before saving as JPEG
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    if im.width > self.config.image_max_width:
                        new_h = int(im.height * self.config.image_max_width / im.width)
                        im = im.resize((self.config.image_max_width, new_h), Image.LANCZOS)
                    parts = split_screenshot(im, self.config.image_part_height,
                                             self.config.image_part_overlap, self.config.image_max_parts)
                    return [_encode(part) for part in parts]
            except Exception as e:
                # Sent unchanged, the image could exceed the size limits above
                self.logger.warning("Left out a screenshot of %s that could not be processed: %s", url, e)
                return None

        def _prepare() -> Tuple[Screenshots, str]:
            screenshots = Screenshots()
            for b64 in images:
                parts = _parts(b64)
                if parts:
                    screenshots.extend(parts)
                    screenshots.split = screenshots.split or len(parts) > 1
            return screenshots, truncate_to_tokens(page_text, self.config.max_text_tokens)

        # Re-encoding large screenshots and counting tokens in the event loop would stall concurrent evaluations
        return await asyncio.to_thread(_prepare)


def _subject(context: dict) -> str:
    """How log messages name the extraction or check that ``context`` describes, starting in lower case.

    An extraction is "the extraction of <template> from <url or the answer>";
    a check is "check <node id>" ("a check without a node" when it has none),
    followed by "against <url>" when it verifies the claim against one page.
    """
    if "template" in context:
        return f"the extraction of {context['template']} from {context.get('url') or 'the answer'}"
    name = f"check {context['node_id']}" if context.get("node_id") else "a check without a node"
    return f"{name} against {context['url']}" if context.get("url") else name


def _capitalized(text: str) -> str:
    return text[:1].upper() + text[1:]


def _check_record(url: Optional[str], passed: bool, votes: Sequence[bool] = (), reasoning: Optional[str] = None,
                  note: Optional[str] = None) -> dict:
    """One entry of ``VerificationNode.evidence["checks"]`` (see :class:`VerificationNode`)."""
    record = {"url": url, "passed": passed, "votes": list(votes), "reasoning": reasoning}
    if note is not None:
        record["note"] = note
    return record


class Extractor(BaseEvaluator):
    """Responsible for structured information extraction from *answer* or URL."""

    GENERAL_PROMPT = text_dedent("""
    You are responsible for extracting specific information of interest from the provided answer text for a task. For context, we are evaluating the correctness of an answer to a web information-gathering task. This extraction step helps us identify relevant information for subsequent validation. You must carefully follow the provided extraction instructions to accurately extract information from the answer.

    GENERAL RULES:
    1. Do not add, omit, or invent any information. Extract only information explicitly mentioned in the provided answer exactly as it appears.
    2. If any required information is missing from the answer, explicitly return `null` as the JSON value.
    3. You will also receive the original task desc as context. Understand it clearly, as it provides essential background for the extraction. You may apply common-sense reasoning to assist your extraction, but your final result must be accurately extracted from the answer text provided.
    4. Occasionally, additional instructions might be provided to aid your extraction. Carefully follow those instructions when available.
    
    
    SPECIAL RULES FOR URL SOURCES EXTRACTION:
    – These rules apply when the request involves extraction of urls sources, for example, the source attribution for a statement.
    1. The sources must be explicitly mentioned in the answer text as URLs. If the answer only provides a description of the source (e.g., "according to Wikipedia" or "as stated on example.com"), but does not provide an actual URL, return `null` for that source.
    2. The sources can be presented in various formats, including plain URLs, markdown links (e.g., `[text](url)`), or embedded within sentences with a dedicated sources section. You must extract the actual URLs. As long as the URLs are presented in a reasonable format, you should be able to extract them.
    
    SPECIAL RULES FOR URL EXTRACTION:
    – These rules apply only when URL fields are required in the extraction.
    1. Extract only URLs explicitly present in the answer text. Do not create or infer any URLs.
    2. Extract only valid URLs. Ignore obviously invalid or malformed URLs.
    3. If a URL is missing a protocol (`http://` or `https://`), prepend `http://`.
    
    Here is the instruction for the extraction for you:
    ```
    {extraction_prompt}
    ```
    
    Here is the original task desc:
    ```
    {task_description}
    ```
    
    Here is the complete answer to the task:
    ```
    {answer}
    ```
    Here are the additional instructions (if any):
    ```
    {additional_instruction}
    ```
    """)

    URL_PROMPT = text_dedent(
        """
        You are responsible for extracting specific information of interest from a webpage (or a PDF file from a PDF webpage). You will receive both the text content and a screenshot of the webpage for examination. For context, we are evaluating the correctness of answers to a web information-gathering task. This extraction step helps us identify relevant information for further validation of the answers. You must carefully follow the provided extraction instructions to accurately extract information from the answer.

        GENERAL RULES:
        1. Do not add, omit, or invent any information. Only extract information explicitly mentioned in the provided answer as it appears.
        2. If any required information is missing from the answer, explicitly return `null` as the JSON value.
        3. You will also receive the original task desc as context. Understand it clearly, as it provides essential background for the extraction. You may apply common-sense reasoning to assist your extraction, but your final result must be accurately extracted from the webpage content provided.
        4. Occasionally, additional instructions might be provided to aid your extraction. Carefully follow those instructions when available.

        SPECIAL RULES FOR URL EXTRACTION:
        – These apply when the extraction requires URL(s) fields.
        1. Only extract URLs explicitly present in the answer text. Do not create or infer any URLs.
        2. Extract only valid and complete URLs. Ignore obviously invalid or malformed URLs.
        3. Always include full URLs, including the prefix protocol. If a URL is missing a protocol (`http://` or `https://`), prepend `http://`.


        Here is the instruction for the extraction for you:
        ```
        {extraction_prompt}
        ```

        Here is the original task desc:
        ```
        {task_description}
        ```

        Here are the additional instructions (if any):
        ```
        {additional_instruction}
        ```

        Below is the plain text extracted from the webpage (truncated if too long):
        ```
        {web_text}
        ```
        """
    )

    def _generate_operation_id(self, operation_type: str) -> str:
        """Generate operation ID"""
        return f"{operation_type}_{uuid.uuid4().hex[:8]}"

    def _build_extract_context(
            self,
            op_id: str,
            extract_type: str,
            template_class: Type[BaseModel],
            prompt: str,
            url: Optional[str] = None,
            use_screenshot: Optional[bool] = None
    ) -> dict:
        """Build extraction context"""
        context = {
            "op_id": op_id,
            "extract_type": extract_type,
            "template": template_class.__name__,
            "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        }

        if url:
            context["url"] = url
        if use_screenshot is not None:
            context["use_screenshot"] = use_screenshot

        return context

    async def _log_and_extract(
            self,
            template_class: Type[BaseModel],
            message_content: Union[str, List[dict]],
            extract_context: dict
    ) -> BaseModel:
        """Run the extraction and log its result, at INFO with the extracted fields as ``result``.

        A rejected request or an error returns an empty extraction; a failed
        judge request (:class:`JudgeError`) and :class:`ContextLengthError` propagate.
        """
        subject = _subject(extract_context)
        try:
            result = await self._core_extract(template_class, message_content)
        except ContextLengthError:
            raise  # the caller sends it again with a shorter page text, or records the rejection
        except JudgeContentError as e:
            self._record_rejection(extract_context, e)
            return empty_extraction(template_class)
        except JudgeError as e:
            self.logger.error(f"{_capitalized(subject)} stopped: a judge request failed for good: {e}",
                              extra={**extract_context, "status": "judge_error"})
            raise
        except Exception as e:
            self.logger.error(f"{_capitalized(subject)} failed, so it counts as empty: {e}",
                              extra={**extract_context, "status": "error"}, exc_info=True)
            return empty_extraction(template_class)

        fields = result.model_dump() if isinstance(result, BaseModel) else result
        self.logger.info(f"Extracted {template_class.__name__} from {extract_context.get('url') or 'the answer'}",
                         extra={**extract_context, "result": fields, "status": "success"})
        return result

    async def _core_extract(
            self,
            template_class: Type[BaseModel],
            message_content: Union[str, List[dict]]
    ) -> BaseModel:
        """Core extraction engine"""

        return await self.call_llm_with_semaphore(
            model=self.MODEL_NAME,
            messages=[{"role": "user", "content": message_content}],
            response_format=template_class,
        )

    async def simple_extract(
            self,
            extraction_prompt: str,
            template_class: Type[BaseModel],
            additional_instruction: str = "None"
    ) -> BaseModel:
        """Extract structured information from answer"""

        # Generate operation ID and context
        op_id = self._generate_operation_id("extract")
        extract_context = self._build_extract_context(
            op_id, "simple", template_class, extraction_prompt
        )

        self.logger.debug(f"Extracting {template_class.__name__} from the answer", extra=extract_context)

        # Build prompt
        prompt = self.GENERAL_PROMPT.format(
            extraction_prompt=extraction_prompt,
            task_description=self.task_description,
            answer=self.answer,
            additional_instruction=additional_instruction
        )

        # Execute extraction
        try:
            return await self._log_and_extract(template_class, prompt, extract_context)
        except ContextLengthError as e:
            self._record_rejection(extract_context, e)
            return empty_extraction(template_class)

    async def extract_from_url(
            self,
            extraction_prompt: str,
            url: str,
            template_class: Type[BaseModel],
            *,
            additional_instruction: str = "None",
            use_screenshot: bool = True,
    ) -> BaseModel:
        """Extract information from URL"""

        # Generate operation ID and context
        op_id = self._generate_operation_id("extract_url")
        extract_context = self._build_extract_context(
            op_id, "url", template_class, extraction_prompt, url, use_screenshot
        )

        self.logger.debug(f"Extracting {template_class.__name__} from {url}", extra=extract_context)
        screenshot_b64, web_text = await self.get_page_info(url)

        if screenshot_b64 is None or web_text is None:
            self.logger.info(f"Extracted nothing from {url}: the page is unavailable", extra=extract_context)
            return empty_extraction(template_class)

        self.logger.debug(f"Loaded {url}: {len(web_text)} characters of text, {len(screenshot_b64)} screenshots",
                          extra={"op_id": op_id})

        def extract(text: str) -> Awaitable[BaseModel]:
            prompt = self.URL_PROMPT.format(
                extraction_prompt=extraction_prompt,
                task_description=self.task_description,
                additional_instruction=additional_instruction,
                web_text=text
            )
            message_content = self._build_message_content(prompt, screenshot_b64, use_screenshot)
            return self._log_and_extract(template_class, message_content, extract_context)

        try:
            return await self._with_shorter_text_on_overflow(web_text, extract, extract_context)
        except ContextLengthError as e:
            self._record_rejection(extract_context, e)
            return empty_extraction(template_class)


class Verifier(BaseEvaluator):
    """Responsible for evidence‑based claim verification."""

    SIMPLE_PROMPT = text_dedent("""
            You are responsible for verifying whether a given claim or simple statement is correct and accurate. Typically, this verification involves straightforward factual judgments or logical checks (e.g., verifying if a given name matches another given name). For context, we are evaluating the correctness of an answer to a web information-gathering task. This verification step helps us determine part of the answer’s accuracy. Your task is to provide a binary judgment ("Correct" or "Incorrect") along with clear and detailed reasoning supporting your decision.

            To assist your judgment, you will also receive:
            - The original task desc (as context).
            - The complete answer to the task (as context).
            - Additional instructions (occasionally provided to guide your verification).

            GENERAL RULES:
            1. Carefully examine the provided claim or statement to verify. Use logic, common sense, or basic reasoning to determine its accuracy.
            2. Clearly understand the provided task desc and complete answer, as they offer important context that may help you better handle variations or edge cases.
            3. Although we provided task desc and the complete answer, you should still focus on the given verification itself. DO NOT conduct any extra verification beyond the claim itself (e.g., verify the URL provenance or any violation to your knowledge). Usually, the verification has been phrased into a very simple logical or factual statement or a simple check. In other words, you should only verify the correctness of the claim itself, do not get distracted by the task desc or the complete answer.
            4. Most of the time, the claim or statement has been phrased into a simple check. If that is the case, you should not rely on your own knowledge or memory about the name or fact itself because those can be false or hallucinated. Instead, you should rely on the provided desc to verify the claim itself. The only exception is when you are explicitly asked to call your own knowledge or memory to conduct the verification.
            5. Your reasoning must be explicit, concise, and directly support your binary judgment.
            6. Carefully follow any additional instructions provided. They are crucial for your verification.
            7. Often the time, it is to check whether something (e.g., a name) matches another thing (e.g., another name). In those cases, you should try your best to allow minor or reasonable variants (e.g., letter casing, minor spelling variations, with or without middle name, etc.) to be considered as a match. Don't be very strict about the exact match.
            8. If the task asks for a number, then reasonable variations or simplifications should be acceptable—for example, rounding 66.7 to 67.

            Here is the original task desc:

            ```
            {task_description}
            ```

            Here is the complete answer to the task:
            ```
            {answer}
            ```

            Here is the claim or the statement to be verified:
            ```
            {claim}
            ```

            Here are the additional instructions (if any):
            ```
            {additional_instruction}
            ```
            """)

    URL_PROMPT = text_dedent("""
                            You are responsible for verifying whether a given claim or "fact" is fully supported by the actual content of a specified webpage (or a PDF file from a PDF webpage). For context, we are examining the correctness of an answer to a web information-gathering task. Typically, the claim or "fact" is extracted directly from the answer, and the webpage provided is the URL source referenced in the answer. This verification step helps us determine whether the claim or "fact" in the answer is accurate or hallucinated, a common issue in LLM-based systems. You will receive both the text content and a screenshot of the webpage for examination. Your task is to provide a binary judgment (i.e., supported or not supported) along with clear and detailed reasoning for your decision.

                            GENERAL RULES:
                            1. The provided webpage content may be lengthy. Carefully examine the relevant sections of both the webpage text and the screenshot. Determine clearly whether the claim or "fact" exactly matches or is explicitly supported by the webpage content. If the information appears to be not able to find from the text, but more likely from the screenshot, please check the screenshot carefully.
                            2. You will also receive the original task desc and the complete answer as context. Understand them clearly, as they provide essential background for evaluating the claim. You may apply common-sense reasoning (e.g., fuzzy matching for names differing only in letter casing or minor spelling variations) to assist your judgment, but your final decision must primarily rely on explicit evidence from the webpage content provided. You should never rely on your own knowledge or memory because those can be false or hallucinated. Instead, you should rely on the information on the webpage. The only exception is when you are explicitly asked to call your own knowledge or memory to conduct the verification.
                            3. Although we provided task desc and the complete answer, you should still focus on the given verification itself. DO NOT conduct any extra verification beyond the claim itself. In other words, you should only verify the correctness of the claim itself, do not get distracted by the task desc or the complete answer.
                            4. If the provided webpage (the URL source mentioned in the answer) is entirely irrelevant, invalid, or inaccessible, you should conclude that the claim or "fact" is not supported.
                            5. Carefully follow any additional instructions provided. They are crucial for your verification.
                            6. Your reasoning must be explicit, concise, and directly support your binary judgment.
                            7. Always allow minor or reasonable variants if the verification is related to some naming or titles (e.g., letter casing, minor spelling variations, with or without middle name, etc.). Don't be very strict about the exact match.
                            8. If the task asks for a number, then reasonable variations or simplifications should be acceptable—for example, rounding 66.7 to 67.
                            
                            Here is the original task desc:

                            ```
                            {task_description}
                            ```

                            Here is the complete answer to the task:
                            ```
                            {answer}
                            ```

                            Here is the claim or the "fact" to be verified:
                            ```
                            {claim}
                            ```

                            Here are the additional instructions (if any):
                            ```
                            {additional_instruction}
                            ```

                            Here is the webpage URL:
                            ```
                            {url}
                            ```
                            
                            Here is the web text extracted from the webpage (truncated if too long):
                            ```
                            {web_text}
                            ```
                            """)

    async def _majority_vote(
            self,
            run_once: Callable[[], Awaitable[BinaryEvalResult]],
            cancellation_event: Optional[asyncio.Event] = None,
            *,
            num_trials: int = 3,
            early_stop: bool = True,
    ) -> Tuple[BinaryEvalResult, List[bool]]:
        """The majority's verdict over up to ``num_trials`` calls of ``run_once``, and every vote cast.

        The verdict returned is the first call whose result agrees with the
        majority.  With ``early_stop``, voting stops once at least two votes
        are cast and either a majority passes or none does.  Raises
        :class:`asyncio.CancelledError` when ``cancellation_event`` is set
        before a call.
        """

        assert num_trials % 2 == 1, "num_trials must be odd!"

        if num_trials <= 1:
            result = await run_once()
            return result, [result.result]

        results = []

        for i in range(num_trials):
            # Check cancellation signal before each attempt
            if cancellation_event and cancellation_event.is_set():
                self.logger.debug(f"Majority vote cancelled after {len(results)} attempts")
                raise asyncio.CancelledError("Verification cancelled by external signal")

            result = await run_once()
            results.append(result)

            # Check early stopping condition
            if early_stop and len(results) >= 2:
                vote_sum = sum(r.result for r in results)
                if (vote_sum > len(results) // 2 or vote_sum == 0):
                    break

        # Calculate final majority result
        final_vote = sum(r.result for r in results) >= (len(results) / 2)
        return next(r for r in results if r.result == final_vote), [r.result for r in results]

    def _process_verify_params(self, **kwargs):
        """Process verification parameters, apply defaults"""
        from types import SimpleNamespace
        return SimpleNamespace(
            additional_instruction=kwargs.get('additional_instruction') or self.config.default_additional_instruction,
            majority_vote=kwargs.get('majority_vote', self.config.default_majority_vote),
            num_trials=kwargs.get('num_trials') or self.config.default_num_trials,
            use_screenshot=kwargs.get('use_screenshot', self.config.default_use_screenshot),
        )

    def _generate_operation_id(self, node: Optional[VerificationNode] = None) -> str:
        """Generate operation ID"""
        if node:
            return f"{node.id}_{uuid.uuid4().hex[:8]}"
        return f"verify_{uuid.uuid4().hex[:8]}"

    def _build_verify_context(
            self,
            op_id: str,
            verify_type: str,
            claim: str,
            node: Optional[VerificationNode] = None,
            url: Optional[str] = None,
            urls: Optional[List[str]] = None,
            node_id: Optional[str] = None,
    ) -> dict:
        """The fields logged with a check's records.

        ``node_id`` is the check's node when ``node`` is None, as for one
        source of a multi-URL check, whose node receives only the overall result.
        """
        context = {
            "op_id": op_id,
            "verify_type": verify_type,
            "node_id": node.id if node else node_id,
            "node_desc": node.desc if node else None,
            "claim": claim,
        }

        if url:
            context["url"] = url
        if urls:
            context["urls"] = urls
            context["url_count"] = len(urls)

        return context

    async def _execute_single_verification(
            self,
            prompt: str,
            message_content: Union[str, List[dict]],
            op_id: str,
            cancellation_event: Optional[asyncio.Event] = None
    ) -> BinaryEvalResult:
        """Execute single verification call"""
        if cancellation_event and cancellation_event.is_set():
            raise asyncio.CancelledError("Verification cancelled before LLM call")

        result = await self.call_llm_with_semaphore(
            model=self.MODEL_NAME,
            messages=[{"role": "user", "content": message_content}],
            response_format=BinaryEvalResult,
        )

        self.logger.debug(f"The judge voted {'pass' if result.result else 'fail'}",
                          extra={"op_id": op_id, "passed": result.result, "reasoning": result.reasoning})

        return result

    async def _core_verify(
            self,
            claim: str,
            prompt: str,
            message_content: Union[str, List[dict]],
            verify_context: dict,
            node: Optional[VerificationNode] = None,
            cancellation_event: Optional[asyncio.Event] = None,
            checks: Optional[list] = None,
            **kwargs
    ) -> bool:
        """Ask the judge (once, or by majority vote), log the outcome, and write it into ``node``.

        The outcome is one INFO record naming the check, with the claim and
        the judge's reasoning, and the votes under majority voting.  Its
        evidence record (:func:`_check_record`) becomes ``node.evidence`` and
        is appended to ``checks`` when given.  A cancelled check is marked
        skipped and re-raises, without a record; an error other than a judge
        failure counts as failed; :class:`JudgeError` and
        :class:`ContextLengthError` propagate.
        """

        op_id = verify_context["op_id"]
        subject = _capitalized(_subject(verify_context))
        params = self._process_verify_params(**kwargs)

        try:
            # Create verification function
            async def _verify_once() -> BinaryEvalResult:
                try:
                    return await self._execute_single_verification(
                        prompt, message_content, op_id, cancellation_event
                    )
                except ContextLengthError:
                    raise  # the caller sends it again with a shorter page text, or records the rejection
                except JudgeContentError as e:
                    # A rejected request is a failed vote; under majority voting the other trials still count
                    self._record_rejection(verify_context, e)
                    return BinaryEvalResult(result=False, reasoning=f"The judge rejected the request: {e}")

            # Execute verification (single or majority vote)
            if params.majority_vote and params.num_trials > 1:
                final_result, votes = await self._majority_vote(
                    _verify_once,
                    cancellation_event,
                    num_trials=params.num_trials
                )
                vote_text = f" ({sum(votes)} of {len(votes)} votes pass)"
            else:
                final_result = await _verify_once()
                votes, vote_text = [final_result.result], ""
            result = final_result.result
            status = "passed" if result else "failed"

            self.logger.info(f"{subject} {status}{vote_text}",
                             extra={**verify_context, "reasoning": final_result.reasoning, "passed": result,
                                    "votes": votes, "status": status})

            self._record_check(_check_record(verify_context.get("url"), result, votes, final_result.reasoning),
                               claim, node, checks)
            if node is not None:
                node.score = 1.0 if result else 0.0
                node.status = status

            return result

        except asyncio.CancelledError:
            status = "skipped"
            self.logger.debug(f"{subject} stopped: another source already verified the claim",
                              extra={**verify_context, "status": status})

            if node is not None:
                node.score = 0.0
                node.status = status
            raise

        except ContextLengthError:
            raise  # the caller sends it again with a shorter page text, or records the rejection

        except JudgeError as e:
            self.logger.error(f"{subject} stopped: a judge request failed for good: {e}",
                              extra={**verify_context, "status": "judge_error"})
            raise

        except Exception as e:
            self.logger.error(f"{subject} failed with an error, so it counts as failed: {e}",
                              extra={**verify_context, "status": "error"}, exc_info=True)
            self._record_check(_check_record(verify_context.get("url"), False, note=f"error: {e}"),
                               claim, node, checks)

            if node is not None:
                node.score = 0.0
                node.status = "failed"
            return False

    @staticmethod
    def _record_check(record: dict, claim: str, node: Optional[VerificationNode],
                      checks: Optional[list]) -> None:
        """Append a check's evidence record to ``checks``, and make it ``node``'s evidence."""
        if checks is not None:
            checks.append(record)
        if node is not None:
            node.evidence = {"claim": claim, "sources": [record["url"]] if record["url"] else [],
                             "checks": [record]}

    async def simple_verify(
            self,
            claim: str,
            node: Optional[VerificationNode] = None,
            cancellation_event: Optional[asyncio.Event] = None,
            op_id: Optional[str] = None,  # Added operation ID parameter
            **kwargs
    ) -> bool:
        """Simple verification"""

        # Use incoming op_id or generate new one
        operation_id = op_id or self._generate_operation_id(node)
        verify_context = self._build_verify_context(operation_id, "simple", claim, node)

        self.logger.debug(f"Checking {_subject(verify_context)} without a source", extra=verify_context)

        # Build prompt
        params = self._process_verify_params(**kwargs)
        prompt = self.SIMPLE_PROMPT.format(
            task_description=self.task_description,
            answer=self.answer,
            claim=claim,
            additional_instruction=params.additional_instruction
        )

        # Call core verification
        try:
            return await self._core_verify(
                claim, prompt, prompt, verify_context, node, cancellation_event, **kwargs
            )
        except ContextLengthError as e:
            return self._fail_too_long(verify_context, e, claim, node, None)

    async def verify_by_url(
            self,
            claim: str,
            url: str,
            node: Optional[VerificationNode] = None,
            cancellation_event: Optional[asyncio.Event] = None,
            op_id: Optional[str] = None,
            node_id: Optional[str] = None,
            checks: Optional[list] = None,
            **kwargs
    ) -> bool:
        """Verify ``claim`` against the page at ``url``; an unavailable page fails the check.

        ``op_id`` identifies the check in the log (generated when None);
        ``node_id`` names the check in the log when ``node`` is None, as for one
        source of a multi-URL check.  The check's evidence record becomes
        ``node.evidence`` and is appended to ``checks`` when given, as
        :meth:`verify_by_urls` collects them.
        """

        operation_id = op_id or self._generate_operation_id(node)
        verify_context = self._build_verify_context(operation_id, "url", claim, node, url=url, node_id=node_id)
        subject = _subject(verify_context)

        self.logger.debug(f"Checking {subject}", extra=verify_context)

        if cancellation_event and cancellation_event.is_set():
            self.logger.debug(f"{_capitalized(subject)} stopped before it started", extra={"op_id": operation_id})
            if node is not None:
                node.score = 0.0
                node.status = "skipped"
            return False

        screenshot_b64, web_text = await self.get_page_info(url, cancellation_event)

        if screenshot_b64 is None or web_text is None:
            self.logger.info(f"{_capitalized(subject)} failed: the page is unavailable",
                             extra={**verify_context, "passed": False, "status": "failed"})
            self._record_check(_check_record(url, False, note="the page is unavailable"), claim, node, checks)
            if node is not None:
                node.score = 0.0
                node.status = "failed"
            return False

        self.logger.debug(f"Loaded {url}: {len(web_text)} characters of text, {len(screenshot_b64)} screenshots",
                          extra={"op_id": operation_id})

        params = self._process_verify_params(**kwargs)

        def verify(text: str) -> Awaitable[bool]:
            prompt = self.URL_PROMPT.format(
                task_description=self.task_description,
                answer=self.answer,
                claim=claim,
                additional_instruction=params.additional_instruction,
                web_text=text,
                url=url
            )
            message_content = self._build_message_content(prompt, screenshot_b64, params.use_screenshot)
            return self._core_verify(
                claim, prompt, message_content, verify_context, node, cancellation_event, checks, **kwargs
            )

        try:
            return await self._with_shorter_text_on_overflow(web_text, verify, verify_context)
        except ContextLengthError as e:
            return self._fail_too_long(verify_context, e, claim, node, checks)

    def _fail_too_long(self, context: dict, error: ContextLengthError, claim: str, node: Optional[VerificationNode],
                       checks: Optional[list]) -> bool:
        """Fail the check that ``context`` describes because its request stayed too long for the judge; return ``False``.

        The rejection is recorded (:meth:`_record_rejection`), the check's
        outcome is logged at INFO like that of any judged check, and its
        evidence record carries the rejection as its note (see
        :meth:`_record_check`, which also appends it to ``checks`` when given).
        """
        self._record_rejection(context, error)
        self.logger.info(f"{_capitalized(_subject(context))} failed: the request was too long for the judge",
                         extra={**context, "passed": False, "status": "failed"})
        self._record_check(_check_record(context.get("url"), False,
                                         note=f"the request was too long for the judge: {error}"),
                           claim, node, checks)
        if node is not None:
            node.score = 0.0
            node.status = "failed"
        return False

    async def verify_by_urls(
            self,
            claim: str,
            urls: List[str],
            node: Optional[VerificationNode] = None,
            op_id: Optional[str] = None,
            **kwargs
    ) -> bool:
        """Verify ``claim`` against each of ``urls`` concurrently; it passes when any one page supports it.

        Each page's check is logged under the node's id; once one passes, the
        others stop, and one INFO record gives the node's overall outcome.
        ``node.evidence`` lists the checks that finished.
        """
        assert urls, "No URLs provided for verification"

        main_op_id = op_id or self._generate_operation_id(node)
        verify_context = self._build_verify_context(main_op_id, "multi_url", claim, node, urls=urls)
        subject = _capitalized(_subject(verify_context))

        self.logger.debug(f"Checking {_subject(verify_context)} against {len(urls)} sources", extra=verify_context)

        cancellation_event = asyncio.Event()
        node_id = verify_context["node_id"]
        checks: list = []

        async def _check_one(url: str, url_index: int) -> tuple[str, bool]:
            sub_op_id = f"{main_op_id}_url_{url_index + 1}"
            try:
                result = await self.verify_by_url(claim, url, None, cancellation_event, op_id=sub_op_id,
                                                  node_id=node_id, checks=checks, **kwargs)
                return url, result
            except asyncio.CancelledError:
                return url, False
            except JudgeError:
                raise
            except Exception as e:
                self.logger.error(f"{subject} against {url} failed with an error, so it counts as failed: {e}",
                                  extra={"op_id": sub_op_id, "url": url}, exc_info=True)
                checks.append(_check_record(url, False, note=f"error: {e}"))
                return url, False

        # Create all tasks
        tasks = [asyncio.create_task(_check_one(url, idx)) for idx, url in enumerate(urls)]

        try:
            # Wait for first successful result
            for checked, coro in enumerate(asyncio.as_completed(tasks), start=1):
                try:
                    url, result = await coro
                    if result:
                        self.logger.info(
                            f"{subject} passed: {url} supports the claim ({checked} of {len(urls)} sources checked)",
                            extra={**verify_context, "verified_by_url": url, "passed": True, "status": "passed"}
                        )

                        # Cancel remaining tasks
                        cancellation_event.set()
                        await asyncio.sleep(0.01)

                        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
                        if cancelled:
                            self.logger.debug(f"Stopped the checks of {cancelled} other sources",
                                              extra={"op_id": main_op_id})

                        # Assign successful result to node
                        if node is not None:
                            node.score = 1.0
                            node.status = "passed"

                        return True
                except asyncio.CancelledError:
                    pass
        except JudgeError:
            # A failed judge request leaves the whole answer unscored; stop the other checks.
            cancellation_event.set()
            for t in tasks:
                t.cancel()
            raise
        finally:
            # Ensure all tasks are completed
            await asyncio.gather(*tasks, return_exceptions=True)
            if node is not None:
                node.evidence = {"claim": claim, "sources": list(urls), "checks": checks}

        self.logger.info(
            f"{subject} failed: none of the {len(urls)} sources supports the claim",
            extra={**verify_context, "passed": False, "status": "failed"}
        )

        #  Assign failed result to node
        if node is not None:
            node.score = 0.0
            node.status = "failed"

        return False


# Factory function
def create_evaluator(
        *,
        client: LLMClient,
        task_description: str,
        answer: str,
        global_cache: CacheFileSys,
        global_semaphore: asyncio.Semaphore,
        logger: logging.Logger,
        default_model: str = DEFAULT_JUDGE_MODEL,
        extract_model: Optional[str] = None,
        verify_model: Optional[str] = None,
        config: Optional[EvaluatorConfig] = None,
        browser_manager: Optional[BatchBrowserManager] = None,
) -> Tuple[Extractor, Verifier]:
    extract_model = extract_model or default_model
    verify_model = verify_model or default_model

    # Extractor and Verifier share one browser: the given one, the one shared
    # through shared_browser(), or a new one that the Extractor owns.
    manager = browser_manager or _shared_browser.get()
    owns_browser = manager is None
    manager = manager or _new_browser()

    common_kwargs = {
        "client": client,
        "task_description": task_description,
        "answer": answer,
        "global_cache": global_cache,
        "global_semaphore": global_semaphore,
        "logger": logger,
        "config": config,
        "browser_manager": manager,
        "usage": JudgeUsage(),  # one answer's judge requests, shared by Extractor and Verifier
    }

    extractor = Extractor(**common_kwargs, model=extract_model)
    verifier = Verifier(**common_kwargs, model=verify_model)
    extractor.owns_browser = owns_browser

    return extractor, verifier
