"""Keyword detection for identifying problematic cached content."""

from __future__ import annotations
import re
import json
from pathlib import Path
from typing import List, Set, Tuple
from dataclasses import dataclass
import logging

from mind2web2.utils.page_info_retrieval import SHORT_PAGE_CHARS, detect_block

logger = logging.getLogger(__name__)


@dataclass
class DetectionResult:
    """Result of keyword detection on text content."""
    has_issues: bool
    matched_keywords: List[str]
    matched_patterns: List[str]
    severity: str  # "definite" or "possible"


class KeywordDetector:
    """Keyword detection with ergonomic config and two severity levels."""
    
    # Definite problem keywords (serious issues)
    DEFAULT_DEFINITE = [
        "this url could not be loaded (navigation error)",
        "robot or human",
        "access denied",
        "403 forbidden", 
        "please complete the verification",
        "cloudflare",
        "blocked",
        "unusual activity",
        "rate limit",
        "verification required",
        "you have been blocked",
        "suspicious activity",
        "security check failed",
        "Spotify is unavailable on this browser.",
        "该网页无法正常运作",
    ]
    
    # Possible problem keywords
    DEFAULT_POSSIBLE = [
        "404 not found",
        "500 internal server error", 
        "site can't be reached",
        "connection timeout",
        "captcha",
        "security check",
        "please verify",
        "anti-bot"
    ]
    
    # Regex patterns for common issues with levels
    DEFAULT_PATTERNS = [
        (r"robot\s+or\s+human", "Robot verification check", "definite"),
        (r"verify\s+you\s+are\s+(not\s+)?a\s+(robot|bot)", "Bot verification", "definite"),
        (r"unusual\s+(traffic|activity)", "Unusual traffic detection", "definite"),
        (r"rate\s+limit|too\s+many\s+requests", "Rate limiting", "possible"),
        (r"access\s+(denied|blocked|restricted)", "Access restriction", "definite"),
        (r"cloudflare|cf-", "Cloudflare protection", "definite"),
        (r"please\s+complete\s+the\s+(verification|captcha)", "Verification required", "definite"),
        (r"this\s+site\s+can't\s+be\s+reached", "Connection failure", "possible")
    ]
    
    def __init__(self, config_path: Path = None):
        self.config_path = config_path
        self.definite_keywords: Set[str] = set()
        self.possible_keywords: Set[str] = set()
        self.patterns: List[Tuple[str, str, str]] = []  # (pattern, description, level)
        
        # Default config path if none provided
        if not self.config_path:
            self.config_path = Path(__file__).resolve().parents[1] / "resources" / "keywords.json"

        self._load_default_rules()
        if self.config_path and self.config_path.exists():
            self._load_config()
    
    def _load_default_rules(self):
        """Load default detection rules."""
        self.definite_keywords.update([k.lower() for k in self.DEFAULT_DEFINITE])
        self.possible_keywords.update([k.lower() for k in self.DEFAULT_POSSIBLE])
        self.patterns.extend(self.DEFAULT_PATTERNS)
        
        logger.debug(f"Loaded default keywords and {len(self.patterns)} patterns")
    
    def _load_config(self):
        """Load configuration from file."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            # Load custom keywords (two levels)
            self.definite_keywords.update([k.lower() for k in config.get('definite', [])])
            self.possible_keywords.update([k.lower() for k in config.get('possible', [])])

            # Load custom patterns
            custom_patterns = config.get('patterns', [])
            for pattern_config in custom_patterns:
                if isinstance(pattern_config, dict) and 'pattern' in pattern_config:
                    pattern = pattern_config['pattern']
                    description = pattern_config.get('description', pattern)
                    level = pattern_config.get('level', 'possible')
                    self.patterns.append((pattern, description, level))
            
            logger.info(f"Loaded custom configuration from {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to load config from {self.config_path}: {e}")
    
    def detect_issues(self, text: str) -> DetectionResult:
        """Detect issues in text content.

        Every matching keyword (sorted, definite ones first) and pattern is
        reported.  The severity is "definite" for empty text, and for a page
        shorter than ``SHORT_PAGE_CHARS`` characters that matches a definite
        keyword or pattern or whose text the crawler's ``detect_block()``
        judges a refusal; it is "possible" otherwise.  A longer page is never
        "definite" by its wording, since articles may quote it; the crawler
        applies the same length threshold when it decides whether a page it
        loaded is a refusal.
        """
        if text is None or not text.strip():
            # Empty or whitespace-only content is a definite issue
            return DetectionResult(True, ["empty content"], [], "definite")

        text_lower = text.lower()
        matched_keywords = sorted(k for k in self.definite_keywords if k in text_lower)
        definite = bool(matched_keywords)
        matched_keywords += sorted(k for k in self.possible_keywords - self.definite_keywords if k in text_lower)
        matched_patterns = []

        for pattern, description, pat_level in self.patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                    matched_patterns.append(description)
                    definite = definite or pat_level == "definite"
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{pattern}': {e}")

        if block := detect_block(None, "", text):
            matched_patterns.append(block)
            definite = True

        has_issues = bool(matched_keywords or matched_patterns)
        severity = "definite" if definite and len(text) < SHORT_PAGE_CHARS else "possible"
        return DetectionResult(has_issues, matched_keywords, matched_patterns, severity)
