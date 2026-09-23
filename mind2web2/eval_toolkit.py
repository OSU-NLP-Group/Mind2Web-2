from __future__ import annotations

import asyncio
import base64
import io
import logging
import random
import textwrap
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, List, Type, Callable, Awaitable, Optional, Tuple, Union

from PIL import Image
from pydantic import BaseModel, ValidationError

from .api_tools import tool_pdf
from .llm_client.base_client import LLMClient
from .llm_client.judge import DEFAULT_JUDGE_MODEL, JudgeError, JudgeUsage
from .utils.cache_filesys import CacheFileSys
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

    That is the case when the page is unavailable or the extraction request
    failed for a reason other than a :class:`JudgeError`.  The result is
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


class EvaluatorConfig:
    """Evaluator configuration settings"""
    max_text_chars: int = 400_000
    image_max_width: int = 1100
    image_max_height: int = 10000
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
        later requests raise :class:`JudgeError` at once without being sent.
        """
        # Use LLM semaphore if available, fallback to default semaphore
        semaphore_to_use = getattr(self.semaphore, 'llm', self.semaphore)
        async with semaphore_to_use:
            if self.usage.failed_requests:
                raise JudgeError("An earlier judge request for this answer failed for good; "
                                 "no further requests are sent for it")
            try:
                result, tokens = await self.client.async_response(count_token=True, **kwargs)
            except JudgeError:
                self.usage.failed_requests += 1
                raise
        self.usage.record(tokens)
        return result

    def _build_message_content(self, prompt: str, screenshot_b64: List[str], use_screenshot: bool = True):
        """Build message content"""
        if use_screenshot and screenshot_b64:
            msg_content = [{"type": "text",
                            "text": prompt + "\n\nBelow are rendered page screenshots to provide non-textual context:"}]
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
            capture = await self.browser_manager.capture(url, self.logger)
        if not capture.ok:
            self.logger.warning(f"Could not capture {url}: {capture.error}")
            await asyncio.to_thread(self.cache.record_failure, url, capture.error, blocked=capture.blocked)
            return None, None
        await asyncio.to_thread(self.cache.put_web, url, capture.text, capture.screenshot_b64)
        return capture.screenshot_b64, capture.text

    async def _fetch_live(self, url: str) -> Tuple[Optional[Union[str, List[str]]], Optional[str]]:
        """Fetch a page that is not cached, store it, and return its screenshots and text.

        A URL that serves a PDF is downloaded; any other URL, including a
        PDF-looking URL whose response is not a PDF, is loaded in the browser.
        """
        if await is_pdf(url):
            pdf_bytes = await self.pdf_parser.fetch(url)
            if pdf_bytes is not None:
                await asyncio.to_thread(self.cache.put_pdf, url, pdf_bytes)
                return await self.pdf_parser.extract(pdf_bytes)
            self.logger.info(f"{url} did not return a PDF; loading it in the browser")
        return await self._capture_and_cache(url)

    async def get_page_info(self, url: str, cancellation_event: Optional[asyncio.Event] = None):
        """The page at ``url`` as ``(screenshots_b64, text)``, or ``(None, None)`` if it is unavailable.

        Cached pages are served from the task's cache.  A URL with a failure
        record in the cache (its capture already failed, during crawling or
        an earlier evaluation) is unavailable, and it is not captured again,
        so that its result does not depend on when the evaluation runs.  Any
        other URL is fetched live and stored (see :meth:`_fetch_live`).
        Screenshots are resized JPEGs; text longer than
        ``config.max_text_chars`` is truncated.
        """

        url = normalize_url_markdown(url)
        self.logger.info(f"🌍Retrieving page info for {url}")
        if cancellation_event and cancellation_event.is_set():
            self.logger.debug(f"Page info retrieval cancelled for {url}")
            return None, None

        content_type = self.cache.has(url)
        if content_type == "pdf":
            screenshot_b64, page_text = await self.pdf_parser.extract(self.cache.get_pdf(url))
        elif content_type == "web":
            page_text, screenshot_bytes = self.cache.get_web(url)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        elif (failure := self.cache.failure(url)) is not None:
            self.logger.warning(f"{url} is unavailable: its capture failed ({failure['reason']})")
            return None, None
        else:
            self.logger.warning(f"No cache for {url}, falling back to live capture")
            screenshot_b64, page_text = await self._fetch_live(url)

        if page_text is None:
            self.logger.warning(f"Failed to retrieve any content for {url}")
            return None, None

        if len(page_text) > self.config.max_text_chars:
            page_text = textwrap.shorten(
                page_text,
                self.config.max_text_chars,
                placeholder="… [CONTENT TRUNCATED]",
            )
        images = screenshot_b64 if isinstance(screenshot_b64, list) else [screenshot_b64]

        def _resize_b64_image(b64_str: str) -> str:
            try:
                data = base64.b64decode(b64_str)
                with Image.open(io.BytesIO(data)) as im:
                    # Pillow PNG/GIF needs to be converted to RGB before saving as JPEG
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")

                    if im.width > self.config.image_max_width:
                        new_h = int(im.height * self.config.image_max_width / im.width)
                        im = im.resize((self.config.image_max_width, new_h), Image.LANCZOS)

                    if im.height > self.config.image_max_height:
                        im = im.crop((0, 0, im.width, self.config.image_max_height))

                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", optimize=True, quality=self.config.jpeg_quality)
                    return base64.b64encode(buf.getvalue()).decode()

            except Exception as e:
                # If error, record and return original image to ensure no interruption
                self.logger.warning("Image resize failed: %s", e)
                return b64_str

        # Re-encoding large screenshots in the event loop would stall concurrent evaluations
        resized = await asyncio.to_thread(lambda: [_resize_b64_image(b64) for b64 in images])
        return resized, page_text


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
        """Execute extraction and log results"""
        op_id = extract_context["op_id"]

        try:
            # Call LLM
            self.logger.debug(f"[{op_id}] Calling LLM for extraction")
            result = await self._core_extract(template_class, message_content)

            # Get result dictionary
            result_dict = result.dict() if hasattr(result, 'dict') else str(result)

            # Log success result
            self.logger.info(
                f"✅ [{op_id}] Extraction completed successfully",
                extra={
                    **extract_context,
                    "result": result_dict,
                    "status": "success"
                }
            )

            return result

        except JudgeError as e:
            self.logger.error(f"❌ [{op_id}] Extraction aborted, judge request failed: {e}",
                              extra={**extract_context, "status": "judge_error", "error": str(e)})
            raise

        except Exception as e:
            self.logger.error(
                f"❌ [{op_id}] Extraction failed: {str(e)}",
                extra={
                    **extract_context,
                    "status": "error",
                    "error": str(e)
                }
            )
            return empty_extraction(template_class)

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

        # Log start
        self.logger.info(
            f"🔍 [{op_id}] Starting extraction from answer using {template_class.__name__}",
            extra=extract_context
        )

        # Build prompt
        prompt = self.GENERAL_PROMPT.format(
            extraction_prompt=extraction_prompt,
            task_description=self.task_description,
            answer=self.answer,
            additional_instruction=additional_instruction
        )

        # Execute extraction
        return await self._log_and_extract(template_class, prompt, extract_context)

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

        # Log start
        self.logger.info(
            f"🔍 [{op_id}] Starting URL extraction from {url} using {template_class.__name__}",
            extra=extract_context
        )

        # Get page info
        self.logger.debug(f"[{op_id}] Fetching page content from {url}")
        screenshot_b64, web_text = await self.get_page_info(url)

        if screenshot_b64 is None or web_text is None:
            self.logger.warning(
                f"[{op_id}] Failed to get page info for URL {url}",
                extra=extract_context
            )
            return empty_extraction(template_class)

        self.logger.debug(
            f"[{op_id}] Page content retrieved: text_length={len(web_text) if web_text else 0}, has_screenshot={bool(screenshot_b64)}"
        )

        # Build prompt
        prompt = self.URL_PROMPT.format(
            extraction_prompt=extraction_prompt,
            task_description=self.task_description,
            additional_instruction=additional_instruction,
            web_text=web_text
        )

        # Build message content
        message_content = self._build_message_content(prompt, screenshot_b64, use_screenshot)

        # Execute extraction
        return await self._log_and_extract(template_class, message_content, extract_context)


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
    ) -> BinaryEvalResult:
        """Majority vote with external cancellation support"""

        assert num_trials % 2 == 1, "num_trials must be odd!"

        if num_trials <= 1:
            return await run_once()

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
        return next(r for r in results if r.result == final_vote)

    def _process_verify_params(self, **kwargs):
        """Process verification parameters, apply defaults"""
        from types import SimpleNamespace
        return SimpleNamespace(
            additional_instruction=kwargs.get('additional_instruction') or self.config.default_additional_instruction,
            majority_vote=kwargs.get('majority_vote', self.config.default_majority_vote),
            num_trials=kwargs.get('num_trials') or self.config.default_num_trials,
            use_screenshot=kwargs.get('use_screenshot', self.config.default_use_screenshot),
        )

    async def _execute_verification(
            self,
            verification_func: Callable[[], Awaitable[BinaryEvalResult]],
            majority_vote: bool,
            num_trials: int,
            cancellation_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Execute verification logic, support external cancellation"""
        if majority_vote and num_trials > 1:
            result = await self._majority_vote(
                verification_func,
                cancellation_event,
                num_trials=num_trials
            )
            return result.result
        else:
            result = await verification_func()
            return result.result

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
            urls: Optional[List[str]] = None
    ) -> dict:
        """Build verification context"""
        context = {
            "op_id": op_id,
            "verify_type": verify_type,
            "id": node.id if node else None,
            "node_desc": node.desc if node else None,
            "claim": claim,
            "claim_preview": claim[:150] + "..." if len(claim) > 150 else claim,
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

        self.logger.debug(f"[{op_id}] Sending request to LLM")

        result = await self.call_llm_with_semaphore(
            model=self.MODEL_NAME,
            messages=[{"role": "user", "content": message_content}],
            response_format=BinaryEvalResult,
        )

        # Log LLM response
        self.logger.debug(
            f"[{op_id}] LLM returned: {'✅ PASS' if result.result else '❌ FAIL'}",
            extra={
                "op_id": op_id,
                "result": result.result,
                "reasoning": result.reasoning
            }
        )

        return result

    async def _core_verify(
            self,
            claim: str,
            prompt: str,
            message_content: Union[str, List[dict]],
            verify_context: dict,
            node: Optional[VerificationNode] = None,
            cancellation_event: Optional[asyncio.Event] = None,
            **kwargs
    ) -> bool:
        """Core verification engine - handle all verification logic and logging"""

        op_id = verify_context["op_id"]
        params = self._process_verify_params(**kwargs)

        # Log verification parameters
        if params.majority_vote and params.num_trials > 1:
            self.logger.debug(
                f"[{op_id}] Verification parameters: majority_vote={params.majority_vote}, trials={params.num_trials}",
                extra={"op_id": op_id, "majority_vote": params.majority_vote, "num_trials": params.num_trials}
            )

        try:
            # Create verification function
            async def _verify_once() -> BinaryEvalResult:
                return await self._execute_single_verification(
                    prompt, message_content, op_id, cancellation_event
                )

            # Execute verification (single or majority vote)
            if params.majority_vote and params.num_trials > 1:
                self.logger.debug(f"[{op_id}] Starting majority vote with {params.num_trials} trials")
                final_result = await self._majority_vote(
                    _verify_once,
                    cancellation_event,
                    num_trials=params.num_trials
                )
                result = final_result.result
                reasoning = final_result.reasoning
            else:
                eval_result = await _verify_once()
                result = eval_result.result
                reasoning = eval_result.reasoning

            # Log final result
            status = "passed" if result else "failed"

            # Build desc
            description = node.desc if node else verify_context.get("claim_preview", "Verification")
            if verify_context.get("url"):
                description += f" @ {verify_context['url']}"

            self.logger.info(
                f"[{op_id}] {'✅ PASSED' if result else '❌ FAILED'} - {description}",
                extra={
                    **verify_context,
                    "result": result,
                    "reasoning": reasoning,
                    "status": status
                }
            )

            # Automatically assign result to node
            if node is not None:
                node.score = 1.0 if result else 0.0
                node.status = status
                self.logger.debug(
                    f"[{op_id}] Updated node status: score={node.score}, status={node.status}"
                )

            return result

        except asyncio.CancelledError:
            status = "skipped"
            description = node.desc if node else "Verification cancelled"
            if verify_context.get("url"):
                description += f" @ {verify_context['url']}"

            self.logger.info(
                f"[{op_id}] ⏭️ SKIPPED - {description}",
                extra={**verify_context, "status": status}
            )

            if node is not None:
                node.score = 0.0
                node.status = status
            raise

        except JudgeError as e:
            self.logger.error(f"[{op_id}] ❌ Verification aborted, judge request failed: {e}",
                              extra={**verify_context, "status": "judge_error", "error": str(e)})
            raise

        except Exception as e:
            status = "error"
            description = node.desc if node else "Verification failed"
            if verify_context.get("url"):
                description += f" @ {verify_context['url']}"

            self.logger.error(
                f"[{op_id}] ❌ ERROR - {description}: {str(e)}",
                extra={**verify_context, "status": status, "error": str(e)}
            )

            if node is not None:
                node.score = 0.0
                node.status = "failed"
            return False

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

        # Log start - use different emoji to avoid repeating with evaluator layer
        self.logger.debug(  # Use debug level, because evaluator layer already has info
            f"   🔍 [{operation_id}] Starting simple verification: {node.desc if node else claim[:100]}",
            extra=verify_context
        )

        # Build prompt
        params = self._process_verify_params(**kwargs)
        prompt = self.SIMPLE_PROMPT.format(
            task_description=self.task_description,
            answer=self.answer,
            claim=claim,
            additional_instruction=params.additional_instruction
        )

        # Call core verification
        return await self._core_verify(
            claim, prompt, prompt, verify_context, node, cancellation_event, **kwargs
        )

    async def verify_by_url(
            self,
            claim: str,
            url: str,
            node: Optional[VerificationNode] = None,
            cancellation_event: Optional[asyncio.Event] = None,
            op_id: Optional[str] = None,  # Added operation ID parameter
            **kwargs
    ) -> bool:
        """Verify by URL"""

        # Use incoming op_id or generate new one
        operation_id = op_id or self._generate_operation_id(node)
        verify_context = self._build_verify_context(operation_id, "url", claim, node, url=url)

        # Log start
        self.logger.debug(
            f"   🌐 [{operation_id}] Starting URL verification: {node.desc if node else claim[:50]}... @ {url}",
            extra=verify_context
        )

        # Check if cancellation has occurred
        if cancellation_event and cancellation_event.is_set():
            self.logger.debug(f"[{op_id}] Already cancelled before start")
            if node is not None:
                node.score = 0.0
                node.status = "skipped"
            return False

        # Get page info
        self.logger.debug(f"[{op_id}] Fetching page content from {url}")
        screenshot_b64, web_text = await self.get_page_info(url, cancellation_event)

        if screenshot_b64 is None or web_text is None:
            self.logger.warning(
                f"[{op_id}] Failed to retrieve page content from {url}",
                extra=verify_context
            )
            if node is not None:
                node.score = 0.0
                node.status = "failed"
            return False

        self.logger.debug(
            f"[{op_id}] Page content retrieved: text_length={len(web_text) if web_text else 0}, has_screenshot={bool(screenshot_b64)}"
        )

        # Build prompt
        params = self._process_verify_params(**kwargs)
        prompt = self.URL_PROMPT.format(
            task_description=self.task_description,
            answer=self.answer,
            claim=claim,
            additional_instruction=params.additional_instruction,
            web_text=web_text,
            url=url
        )

        message_content = self._build_message_content(prompt, screenshot_b64, params.use_screenshot)

        # Call core verification
        return await self._core_verify(
            claim, prompt, message_content, verify_context, node, cancellation_event, **kwargs
        )

    async def verify_by_urls(
            self,
            claim: str,
            urls: List[str],
            node: Optional[VerificationNode] = None,
            op_id: Optional[str] = None,  # Added operation ID parameter
            **kwargs
    ) -> bool:
        """Multi-URL verification"""
        assert urls, "No URLs provided for verification"

        # Generate operation ID and context
        main_op_id = op_id or self._generate_operation_id(node)
        verify_context = self._build_verify_context(main_op_id, "multi_url", claim, node, urls=urls)

        # Log start
        self.logger.debug(
            f"   🔗 [{main_op_id}] Starting multi-URL verification ({len(urls)} URLs): {node.desc if node else claim[:50]}...",
            extra=verify_context
        )

        cancellation_event = asyncio.Event()

        async def _check_one(url: str, url_index: int) -> tuple[str, bool]:
            # Generate sub-op_id, based on main op_id
            sub_op_id = f"{main_op_id}_url_{url_index + 1}"

            try:
                self.logger.debug(
                    f"     🔸 [{sub_op_id}] Checking URL {url_index + 1}/{len(urls)}: {url}",
                    extra={"op_id": sub_op_id, "parent_op_id": main_op_id, "url": url, "url_index": url_index}
                )

                # Pass sub-op_id to single URL verification
                result = await self.verify_by_url(claim, url, None, cancellation_event, op_id=sub_op_id, **kwargs)

                self.logger.debug(
                    f"     {'✅' if result else '❌'} [{sub_op_id}] URL {url_index + 1} result: {'PASS' if result else 'FAIL'}",
                    extra={"op_id": sub_op_id, "parent_op_id": main_op_id, "url": url, "result": result}
                )

                return url, result
            except asyncio.CancelledError:
                self.logger.debug(f"     ⏭️ [{sub_op_id}] Verification cancelled")
                return url, False
            except JudgeError:
                raise
            except Exception as e:
                self.logger.error(f"     ❌ [{sub_op_id}] Error verifying URL: {e}")
                return url, False

        # Create all tasks
        tasks = [asyncio.create_task(_check_one(url, idx)) for idx, url in enumerate(urls)]

        try:
            # Wait for first successful result
            for coro in asyncio.as_completed(tasks):
                try:
                    url, result = await coro
                    if result:
                        self.logger.info(
                            f"[{op_id}] ✅ FOUND - Claim verified by URL: {url}",
                            extra={**verify_context, "verified_by_url": url, "status": "passed"}
                        )

                        # Cancel remaining tasks
                        cancellation_event.set()
                        await asyncio.sleep(0.01)

                        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
                        if cancelled:
                            self.logger.debug(f"[{op_id}] Cancelled {cancelled} remaining verification task(s)")

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

        # No verification found
        self.logger.info(
            f"[{op_id}] ❌ NOT FOUND - Claim not verified by any of {len(urls)} URLs",
            extra={**verify_context, "urls_checked": len(urls), "status": "failed"}
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
