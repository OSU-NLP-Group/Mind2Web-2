from __future__ import annotations

import os
from datetime import datetime
from typing import Literal, List, Dict, Any, Union
from urllib.parse import urldefrag, quote, unquote, quote_plus
from functools import lru_cache

import dill

CacheItemType = Literal["text", "screenshot", "pdf"]


class CacheClass:
    """In‑memory cache with optional disk persistence and mutation log.

    * Every **put_*** call appends a *put* record to ``change_log``.
    * ``remove_url()`` can now delete selected artefacts (or all) & logs *remove*.
    * Utility helpers (`has_*`, `summary`, etc.) ease cache inspection.
    * Improved URL matching handles encoding/decoding differences.
    """

    # ──────────────────────────────── INIT ────────────────────────────────
    def __init__(
            self,
            cache_path: str | None = None,
    ) -> None:
        self.text_cache: Dict[str, str] = {}
        self.screenshot_cache: Dict[str, str] = {}
        self.pdf_cache: Dict[str, bytes] = {}
        self.change_log: List[Dict[str, Any]] = []
        self.cache_path = cache_path

        if cache_path:
            try:
                print("loading cache from {}".format(cache_path))
                self.load(cache_path)
                print(self.summary())
            except Exception as e:  # pylint: disable=broad-except
                print(f"Warning: Unable to load cache: {e}, creating new cache")

    # ─────────────────────────── INTERNAL HELPERS ─────────────────────────
    @staticmethod
    def _now_iso() -> str:
        return datetime.utcnow().isoformat(timespec="microseconds") + "Z"

    def _log(self, *, action: str, url: str, artefact_type: CacheItemType | None = None):
        self.change_log.append(
            {
                "ts": self._now_iso(),
                "action": action,
                "type": artefact_type,
                "url": url,
            }
        )

    def _normalize_url(self, url: str) -> str:
        """Normalize URL to a consistent format for storage"""
        # 1. Remove fragment
        url_no_frag, _ = urldefrag(url)
        # 2. URL decode
        decoded = unquote(url_no_frag)
        # 3. Remove trailing slash (except for root path)
        if decoded.endswith('/') and len(decoded) > 1 and not decoded.endswith('://'):
            decoded = decoded[:-1]
        return decoded



    @lru_cache(maxsize=1000)
    def _get_url_variants(self, url: str) -> List[str]:
        """Generate all possible variants of URL (with/without fragment, with/without trailing slash, encoded/decoded)"""

        def format_url(u: str) -> str:
            # Remove specific utm_source
            return u.replace("?utm_source=chatgpt.com", "") if u.endswith("?utm_source=chatgpt.com") else u

        def swap_scheme(u: str):
            if u.startswith("http://"):
                return "https://" + u[7:]
            if u.startswith("https://"):
                return "http://" + u[8:]
            return None

        url_no_frag, _ = urldefrag(url)

        base_urls: set[str] = {
            url,
            url_no_frag,
            format_url(url),
            format_url(url_no_frag),
            f"{url}?utm_source=chatgpt.com",
            f"{url_no_frag}?utm_source=chatgpt.com",
        }

        # Trailing slash variants
        if not url.endswith("/"):
            base_urls.add(f"{url}/?utm_source=chatgpt.com")
        if not url_no_frag.endswith("/"):
            base_urls.add(f"{url_no_frag}/?utm_source=chatgpt.com")

        # remove the "www." prefix variant
        if url.startswith("http://www."):
            base_urls.add("http://" + url[11:])
        if url.startswith("https://www."):
            base_urls.add("https://" + url[12:])

        # Do http/https swap for all current URLs
        for u in list(base_urls):  # Note: use list here to avoid set size change during iteration
            swapped = swap_scheme(u)
            if swapped:
                base_urls.add(swapped)
        variants=[]
        for base_url in base_urls:
            # Add encoded and decoded versions
            try:
                original = base_url

                # Strategy 2: Default encoding (only keep alphanumeric and -._~)
                encoded_default = quote(base_url)

                # Strategy 3: Keep basic URL structure
                encoded_basic = quote(base_url, safe=':/?#')

                # Strategy 4: Keep basic structure + common characters
                encoded_common = quote(base_url, safe=':/?#@!$&\'*+,;=')

                # Strategy 5: Keep more characters (including brackets [] but not parentheses ())
                encoded_brackets = quote(base_url, safe=':/?#[]@!$&\'*+,;=')

                # Strategy 6: RFC3986 complete safe character set
                encoded_rfc = quote(base_url, safe=':/?#[]@!$&\'()*+,;=')

                # Strategy 7: Minimal encoding (only keep protocol separators)
                encoded_minimal = quote(base_url, safe=':/')

                # Strategy 8: Special space handling (use quote_plus, spaces become + signs)
                encoded_plus = quote_plus(base_url, safe=':/?#[]@!$&\'()*+,;=')

                # Strategy 9: Decode
                decoded_url = unquote(base_url)

                # All encoding variants
                encoding_variants = [
                    original,  # Original
                    encoded_default,  # Default encoding
                    encoded_basic,  # Basic structure
                    encoded_common,  # Common characters
                    encoded_brackets,  # Including brackets
                    encoded_rfc,  # RFC3986 complete
                    encoded_minimal,  # Minimal encoding
                    encoded_plus,  # quote_plus
                    decoded_url,  # Decoded
                ]

                # For each encoded state URL, add trailing slash variants
                for url_variant in encoding_variants:
                    variants.append(url_variant)

                    # Add or remove trailing slash
                    if url_variant.endswith("/") and len(url_variant) > 1 and not url_variant.endswith('://'):
                        variants.append(url_variant[:-1])  # Remove trailing slash
                    elif not url_variant.endswith('/'):
                        variants.append(url_variant + "/")  # Add trailing slash
            except Exception as e:
                print(f"Cache Error ❌ {e}")
                # If encoding/decoding fails, at least keep the original URL
                variants.append(base_url)
                if base_url.endswith("/") and len(base_url) > 1 and not base_url.endswith('://'):
                    variants.append(base_url[:-1])  # Remove trailing slash
                elif not base_url.endswith('/'):
                    variants.append(base_url + "/")  # Add trailing slash

        # Deduplicate and maintain order
        seen = set()
        unique_variants = []
        for variant in variants:
            if variant not in seen:
                seen.add(variant)
                unique_variants.append(variant)

        return unique_variants

    def _find_in_cache(self, url: str, cache_dict: Dict[str, Any]) -> Any | None:
        """Find any variant of URL in specified cache dictionary"""
        # Step 1: Direct lookup of normalized URL (most common case)

        url = url
        if url in cache_dict:
            return cache_dict[url]

        normalized_url = self._normalize_url(url)
        if normalized_url in cache_dict:
            return cache_dict[normalized_url]

        # Step 2: Find all variants of input URL
        variants = self._get_url_variants(url)
        for variant in variants:
            if variant in cache_dict:
                return cache_dict[variant]

        # Step 3: If not found, perform reverse search (compare normalized URLs)
        normalized_input = self._normalize_url(url)
        for cached_url, cached_value in cache_dict.items():
            try:
                normalized_cached = self._normalize_url(cached_url)
                if normalized_input == normalized_cached:
                    return cached_value
            except Exception:
                # If normalization fails, skip this entry
                continue

        return None

    def _has_in_cache(self, url: str, cache_dict: Dict[str, Any]) -> bool:
        """Check if any variant of URL exists in specified cache dictionary"""
        # Step 1: Direct lookup of normalized URL (most common case)
        normalized_url = self._normalize_url(url)
        if normalized_url in cache_dict:
            return True

        # Step 2: Find all variants of input URL
        variants = self._get_url_variants(url)
        for variant in variants:
            if variant in cache_dict:
                return True

        # Step 3: If not found, perform reverse search (compare normalized URLs)
        normalized_input = self._normalize_url(url)
        for cached_url in cache_dict:
            try:
                normalized_cached = self._normalize_url(cached_url)
                if normalized_input == normalized_cached:
                    return True
            except Exception:
                # If normalization fails, skip this entry
                continue

        return False

    # ───────────────────────────── PUT / GET ─────────────────────────────
    def put_text(self, url: str, text: str):
        normalized_url = self._normalize_url(url)
        self.text_cache[normalized_url] = text
        self._log(action="put", url=normalized_url, artefact_type="text")

    def get_text(self, url: str) -> str | None:
        result = self._find_in_cache(url, self.text_cache)
        if result is None:
            print(f"[CacheClass] get_text: no matching artefacts for {url}")
        return result

    def put_screenshot(self, url: str, screenshot_b64: str):
        normalized_url = self._normalize_url(url)
        self.screenshot_cache[normalized_url] = screenshot_b64
        self._log(action="put", url=normalized_url, artefact_type="screenshot")

    def get_screenshot(self, url: str) -> str | None:
        result = self._find_in_cache(url, self.screenshot_cache)
        if result is None:
            print(f"[CacheClass] get_screenshot: no matching artefacts for {url}")
        return result

    def put_pdf(self, url: str, pdf_bytes: bytes):
        normalized_url = self._normalize_url(url)
        self.pdf_cache[normalized_url] = pdf_bytes
        self._log(action="put", url=normalized_url, artefact_type="pdf")

    def get_pdf(self, url: str) -> bytes | None:
        result = self._find_in_cache(url, self.pdf_cache)
        if result is None:
            print(f"[CacheClass] get_pdf: no matching artefacts for {url}")
        return result

    # ───────────────────────────── REMOVE ────────────────────────────────
    def remove_url(self, url: str, *types: CacheItemType | Literal["all"], silent: bool = False):
        """Delete cached artefacts for *url*.

        * If ``types`` empty **or** contains "all" → remove from every cache.
        * Otherwise, only delete specified artefact types.
        * Each removal is recorded in ``change_log``.
        """
        if not types or "all" in types:
            types = ("text", "screenshot", "pdf")  # type: ignore

        cache_mapping = {
            "text": self.text_cache,
            "screenshot": self.screenshot_cache,
            "pdf": self.pdf_cache,
        }

        removed_any = False
        for cache_type in types:
            cache_dict = cache_mapping[cache_type]

            # First try to directly delete the normalized URL
            normalized_url = self._normalize_url(url)
            if normalized_url in cache_dict:
                cache_dict.pop(normalized_url)
                self._log(action="remove", url=normalized_url, artefact_type=cache_type)
                removed_any = True
                continue

            # Find all variants of the URL and delete the ones found
            variants = self._get_url_variants(url)
            for variant in variants:
                if variant in cache_dict:
                    cache_dict.pop(variant)
                    self._log(action="remove", url=variant, artefact_type=cache_type)
                    removed_any = True

            # If not found yet, perform reverse search
            if not removed_any:
                normalized_input = self._normalize_url(url)
                to_remove = []
                for cached_url in cache_dict:
                    try:
                        normalized_cached = self._normalize_url(cached_url)
                        if normalized_input == normalized_cached:
                            to_remove.append(cached_url)
                    except Exception:
                        continue

                for cached_url in to_remove:
                    cache_dict.pop(cached_url)
                    self._log(action="remove", url=cached_url, artefact_type=cache_type)
                    removed_any = True

        if not removed_any and not silent:
            print(f"[CacheClass] remove_url: no matching artefacts for {url}")

    # ───────────────────────────── QUERY API ─────────────────────────────
    def has_text(self, url: str) -> bool:
        return self._has_in_cache(url, self.text_cache)

    def has_screenshot(self, url: str) -> bool:
        return self._has_in_cache(url, self.screenshot_cache)

    def has_pdf(self, url: str) -> bool:
        return self._has_in_cache(url, self.pdf_cache)

    def has(self, url: str, *types: CacheItemType) -> bool:
        if not types:
            types = ("text", "screenshot", "pdf")  # type: ignore

        checkers = {
            "text": self.has_text,
            "screenshot": self.has_screenshot,
            "pdf": self.has_pdf,
        }

        return any(checkers[cache_type](url) for cache_type in types)

    def get_all_urls(self) -> List[str]:
        return sorted(
            set(self.text_cache)
            | set(self.screenshot_cache)
            | set(self.pdf_cache)
        )

    # ────────────────────────────  SUMMARY ───────────────────────────────
    def summary(self, include_log: bool = False, include_url: bool = False) -> Dict[str, Any]:
        text_n, ss_n, pdf_n = self.get_size()
        return {
            "entries": {
                "text": text_n,
                "screenshot": ss_n,
                "pdf": pdf_n,
            },
            "total_urls": len(self.get_all_urls()),
            "log_len": len(self.change_log),
            "change_log": self.change_log if include_log else None,
            'URLs': self.get_all_urls() if include_url else None,
        }

    # ─────────────────────────── PERSISTENCE ─────────────────────────────
    def dump(self, cache_path: str):
        text_cache_sorted = {k: self.text_cache[k] for k in sorted(self.text_cache)}
        screenshot_cache_sorted = {k: self.screenshot_cache[k] for k in sorted(self.screenshot_cache)}
        pdf_cache_sorted = {k: self.pdf_cache[k] for k in sorted(self.pdf_cache)}

        state = {
            "text_cache": text_cache_sorted,
            "screenshot_cache": screenshot_cache_sorted,
            "pdf_cache": pdf_cache_sorted,
            "change_log": self.change_log,
        }
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "wb") as f:
            dill.dump(state, f)

    def save(self):
        before_saving_summary = self.summary()

        if self.cache_path:
            cache_path = self.cache_path
            self.cache_path = None
            self.dump(cache_path)
        after_saving_summary = self.summary()
        print(
            f"{'#' * 10} Before saving: {cache_path} {'#' * 10}\n"
            f"{before_saving_summary}\n"
            f"{'#' * 10} After saving: {cache_path} {'#' * 10}\n"
            f"{after_saving_summary}"
        )

    def load(self, cache_path: str):
        # The cache file must exist
        if not os.path.exists(cache_path):
            raise FileNotFoundError(f"Cache file not found: {cache_path}")

        with open(cache_path, "rb") as f:
            state = dill.load(f)
        self.text_cache = state.get("text_cache", {})
        self.screenshot_cache = state.get("screenshot_cache", {})
        self.pdf_cache = state.get("pdf_cache", {})
        self.change_log = state.get("change_log", [])

    # ───────────────────────────── OTHER UTILS ───────────────────────────
    def clear(self):
        self.text_cache.clear()
        self.screenshot_cache.clear()
        self.pdf_cache.clear()
        self.change_log.clear()
        # Clear LRU cache
        self._get_url_variants.cache_clear()

    def clear_log(self):
        self.change_log.clear()

    def get_size(self) -> tuple[int, int, int]:
        return (
            len(self.text_cache),
            len(self.screenshot_cache),
            len(self.pdf_cache),
        )

    # ──────────────────────────── MERGE ──────────────────────────────────
    def merge(
            self,
            other: Union["CacheClass", str],
            prefer: Literal["self", "other"] = "other",
            *,
            merge_log: bool = True,
    ) -> None:
        """Merge *other* into **this** instance, in place.

        Parameters
        ----------
        other : CacheClass | str
            * ``CacheClass`` → merge its in-memory dictionaries directly.
            * ``str``        → treat as a disk path, ``load()`` into a temporary
              instance first, then merge.
        prefer : {"self", "other"}, default "other"
            Conflict policy for duplicate URLs: keep values from *self* or *other*?
        merge_log : bool, default True
            If ``True``, append ``other``'s ``change_log`` and sort the combined
            log by timestamp.

        Notes
        -----
        * The merge is **in-place**; no new object is returned.
        * *other* is never modified. For a path, the temporary instance is discarded
          after merging.
        """
        # Step 1 – obtain a CacheClass object from *other*
        if isinstance(other, CacheClass):
            src = other
        elif isinstance(other, str):
            src = CacheClass()
            src.load(other)
        else:
            raise TypeError("`other` must be CacheClass or str path")

        # Step 2 – helper to merge individual dictionaries
        def _merge_dict(dst: dict, src_dict: dict):
            for k, v in src_dict.items():
                # Normalize URL for comparison
                normalized_k = self._normalize_url(k)

                # Check if a URL with the same normalized form already exists
                existing_key = None
                for existing in dst:
                    if self._normalize_url(existing) == normalized_k:
                        existing_key = existing
                        break

                if prefer == "other" or existing_key is None:
                    # If an old key exists, delete it first
                    if existing_key is not None:
                        dst.pop(existing_key)
                    # Store using the normalized key
                    dst[normalized_k] = v

        # Step 3 – merge each artefact cache
        _merge_dict(self.text_cache, src.text_cache)
        _merge_dict(self.screenshot_cache, src.screenshot_cache)
        _merge_dict(self.pdf_cache, src.pdf_cache)

        # Step 4 – optionally merge logs
        if merge_log:
            self.change_log.extend(src.change_log)
            self.change_log.sort(key=lambda rec: rec["ts"])

        # Step 5 – (optional) diagnostics
        # print("[CacheClass] merge complete →", self.summary())

    # ───────────────────────────── URL UTILITIES ─────────────────────────
    def get_original_url(self, normalized_url: str) -> str:
        """Get original URL corresponding to normalized URL (find latest put operation from change_log)"""
        for log_entry in reversed(self.change_log):
            if log_entry["action"] == "put" and self._normalize_url(log_entry["url"]) == normalized_url:
                return log_entry["url"]
        return normalized_url  # If not found, return the normalized URL

    def debug_url_variants(self, url: str) -> Dict[str, Any]:
        """Debug tool: display all variants of URL and matching status"""
        normalized = self._normalize_url(url)
        variants = self._get_url_variants(url)

        debug_info = {
            "input_url": url,
            "normalized_url": normalized,
            "variants": variants,
            "cache_status": {
                "text": self.has_text(url),
                "screenshot": self.has_screenshot(url),
                "pdf": self.has_pdf(url),
            }
        }

        return debug_info
