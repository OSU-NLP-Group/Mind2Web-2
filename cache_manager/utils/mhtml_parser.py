"""MHTML file parsing and processing utilities."""

from __future__ import annotations
import base64
import email
import mimetypes
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class MHTMLParser:
    """Parser for MHTML files with content extraction."""
    
    def __init__(self):
        self.parser = BytesParser(policy=policy.default)
    
    def parse_file(self, file_path: str | Path) -> Optional['MHTMLContent']:
        """Parse MHTML file and extract content.
        
        Args:
            file_path: Path to MHTML file
            
        Returns:
            MHTMLContent object or None if parsing failed
        """
        try:
            with open(file_path, 'rb') as f:
                data = f.read()
            return self.parse_bytes(data)
        except Exception as e:
            logger.error(f"Failed to parse MHTML file {file_path}: {e}")
            return None
    
    def parse_bytes(self, data: bytes) -> Optional['MHTMLContent']:
        """Parse MHTML from bytes data.
        
        Args:
            data: MHTML file data as bytes
            
        Returns:
            MHTMLContent object or None if parsing failed
        """
        try:
            # Handle different encodings
            text_data = self._decode_bytes(data)
            if not text_data:
                logger.error("Failed to decode MHTML data")
                return None
            
            # Parse as email message
            msg = self.parser.parsebytes(text_data.encode('utf-8', errors='replace'))
            
            return self._extract_content(msg)
            
        except Exception as e:
            logger.error(f"Failed to parse MHTML bytes: {e}")
            return None
    
    def _decode_bytes(self, data: bytes) -> Optional[str]:
        """Try to decode bytes with various encodings."""
        encodings = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
        
        for encoding in encodings:
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        
        # Fallback: decode with errors='replace'
        try:
            return data.decode('utf-8', errors='replace')
        except Exception:
            return None
    
    def _extract_content(self, msg) -> 'MHTMLContent':
        """Extract content from parsed email message."""
        content = MHTMLContent()
        
        # Extract main URL from Subject or Content-Location
        main_url = self._extract_main_url(msg)
        content.main_url = main_url
        
        # Process all parts
        for part in msg.walk():
            self._process_part(part, content)
        
        # Post-process HTML content
        if content.html_content:
            content.text_content = self._html_to_text(content.html_content)
        
        return content
    
    def _extract_main_url(self, msg) -> str:
        """Extract the main URL from message headers."""
        # Try Subject header first (common in saved pages)
        subject = msg.get('Subject', '')
        if subject and subject.startswith('http'):
            return subject.split()[0]  # Take first URL-like string
        
        # Try Content-Location
        content_location = msg.get('Content-Location', '')
        if content_location and content_location.startswith('http'):
            return content_location
        
        # Look in message parts for main HTML
        for part in msg.walk():
            content_location = part.get('Content-Location', '')
            if content_location and content_location.startswith('http'):
                content_type = part.get_content_type()
                if content_type == 'text/html':
                    return content_location
        
        return ""
    
    def _process_part(self, part, content: 'MHTMLContent'):
        """Process a single message part."""
        content_type = part.get_content_type()
        content_location = part.get('Content-Location', '')
        
        try:
            if content_type == 'text/html':
                # Main HTML content
                html_data = part.get_content()
                if isinstance(html_data, str):
                    content.html_content = html_data
                elif isinstance(html_data, bytes):
                    content.html_content = html_data.decode('utf-8', errors='replace')
            
            elif content_type.startswith('image/'):
                # Image content
                image_data = part.get_content()
                if isinstance(image_data, str):
                    # Base64 encoded
                    try:
                        image_bytes = base64.b64decode(image_data)
                        content.images[content_location] = image_bytes
                    except Exception:
                        pass
                elif isinstance(image_data, bytes):
                    content.images[content_location] = image_data
            
            elif content_type == 'text/css':
                # CSS content
                css_data = part.get_content()
                if isinstance(css_data, str):
                    content.stylesheets[content_location] = css_data
                elif isinstance(css_data, bytes):
                    content.stylesheets[content_location] = css_data.decode('utf-8', errors='replace')
            
            elif content_type == 'application/javascript' or content_type == 'text/javascript':
                # JavaScript content
                js_data = part.get_content()
                if isinstance(js_data, str):
                    content.scripts[content_location] = js_data
                elif isinstance(js_data, bytes):
                    content.scripts[content_location] = js_data.decode('utf-8', errors='replace')
        
        except Exception as e:
            logger.debug(f"Failed to process part {content_type}: {e}")
    
    def _html_to_text(self, html: str) -> str:
        """Convert HTML content to plain text."""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Remove script and style elements
            for script in soup(['script', 'style', 'meta', 'link']):
                script.decompose()
            
            # Get text content
            text = soup.get_text('\n', strip=True)
            
            # Clean up whitespace
            lines = [line.strip() for line in text.split('\n')]
            lines = [line for line in lines if line]  # Remove empty lines
            
            return '\n'.join(lines)
        
        except Exception as e:
            logger.error(f"Failed to convert HTML to text: {e}")
            return ""
    
    def get_main_image(self, content: 'MHTMLContent') -> Optional[bytes]:
        """Extract the main/largest image from MHTML content.
        
        Args:
            content: MHTMLContent object
            
        Returns:
            Image bytes or None if no suitable image found
        """
        if not content.images:
            return None
        
        # Find largest image by file size
        largest_image = None
        largest_size = 0
        
        for location, image_data in content.images.items():
            if len(image_data) > largest_size:
                largest_size = len(image_data)
                largest_image = image_data
        
        # Only return if reasonably large (likely screenshot)
        if largest_size > 10000:  # At least 10KB
            return largest_image
        
        return None


class MHTMLContent:
    """Container for parsed MHTML content."""
    
    def __init__(self):
        self.main_url: str = ""
        self.html_content: str = ""
        self.text_content: str = ""
        self.images: Dict[str, bytes] = {}  # location -> image_data
        self.stylesheets: Dict[str, str] = {}  # location -> css_content
        self.scripts: Dict[str, str] = {}  # location -> js_content
    
    def get_page_title(self) -> str:
        """Extract page title from HTML content."""
        if not self.html_content:
            return ""
        
        try:
            soup = BeautifulSoup(self.html_content, 'html.parser')
            title_tag = soup.find('title')
            if title_tag:
                return title_tag.get_text().strip()
        except Exception:
            pass
        
        return ""
    
    def get_meta_description(self) -> str:
        """Extract meta description from HTML content."""
        if not self.html_content:
            return ""
        
        try:
            soup = BeautifulSoup(self.html_content, 'html.parser')
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                return meta_desc.get('content').strip()
        except Exception:
            pass
        
        return ""
    
    def get_image_count(self) -> int:
        """Get number of images in MHTML."""
        return len(self.images)
    
    def get_largest_image(self) -> Optional[bytes]:
        """Get the largest image (likely the main screenshot)."""
        parser = MHTMLParser()
        return parser.get_main_image(self)
    
    def has_valid_content(self) -> bool:
        """Check if MHTML has valid content."""
        return bool(self.html_content or self.text_content or self.images)
    
    def __str__(self) -> str:
        return (f"MHTMLContent(url='{self.main_url}', "
                f"html_size={len(self.html_content)}, "
                f"text_size={len(self.text_content)}, "
                f"images={len(self.images)}, "
                f"stylesheets={len(self.stylesheets)}, "
                f"scripts={len(self.scripts)})")


# Convenience functions
def parse_mhtml_file(file_path: str | Path) -> Optional[MHTMLContent]:
    """Parse MHTML file (convenience function).
    
    Args:
        file_path: Path to MHTML file
        
    Returns:
        MHTMLContent object or None if parsing failed
    """
    parser = MHTMLParser()
    return parser.parse_file(file_path)


def extract_text_and_image(mhtml_path: str | Path) -> Tuple[str, str, Optional[bytes]]:
    """Extract URL, text content, and main image from MHTML file.
    
    Args:
        mhtml_path: Path to MHTML file
        
    Returns:
        Tuple of (url, text_content, image_bytes)
    """
    content = parse_mhtml_file(mhtml_path)
    if not content:
        return "", "", None
    
    url = content.main_url
    text = content.text_content
    image = content.get_largest_image()
    
    return url, text, image