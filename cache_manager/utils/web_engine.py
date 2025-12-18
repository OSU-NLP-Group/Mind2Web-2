"""Web engine for live loading, screenshots, and content extraction."""

from __future__ import annotations
import asyncio
import tempfile
from pathlib import Path
from typing import Tuple, Optional
import logging

from PySide6.QtCore import QUrl, QTimer, QEventLoop, QSize, QObject, Signal, Qt, QBuffer, QIODevice
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEngineSettings,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineScriptCollection,
    QWebEngineUrlRequestInterceptor,
)

from email import policy
from email.parser import BytesParser
from bs4 import BeautifulSoup
from typing import Tuple

logger = logging.getLogger(__name__)


class WebEngine(QObject):
    """Web engine for loading URLs and capturing content."""
    
    # Fixed viewport size for consistent screenshots
    VIEWPORT_SIZE = QSize(1100, 6000)   # full-page capture height (e.g., MHTML)
    BASE_VIEWPORT_SIZE = QSize(1100, 800)  # default live/viewport capture size
    MAX_CAPTURE_TRIES = 5
    CAPTURE_INTERVAL_MS = 1000
    
    # Signals for async operations
    load_finished = Signal(bool)
    content_ready = Signal(str, bytes)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.web_view: Optional[QWebEngineView] = None
        self._full_capture: bool = False
        self.setup_web_view()
        
    def setup_web_view(self):
        """Setup the web view with optimal settings."""
        self.web_view = QWebEngineView()
        # Use shared persistent profile
        profile = _ensure_shared_profile()
        page = QWebEnginePage(profile, self.web_view)
        self.web_view.setPage(page)
        self.web_view.setAttribute(Qt.WA_DontShowOnScreen, True)
        # Default to a realistic on-screen viewport for live/viewport capture
        self.web_view.resize(self.BASE_VIEWPORT_SIZE)
        self.web_view.page().setBackgroundColor(Qt.white)
        self.web_view.show()
        
        # Configure web view settings
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.AutoLoadImages, True)
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.PluginsEnabled, False)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.AllowRunningInsecureContent, True)
        
        # Do not fix to a giant size by default; full-page captures will resize temporarily
        
        # Enable local content access on profile as well
        profile.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        profile.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        
        # Connect signals
        self.web_view.loadFinished.connect(self._on_load_finished)
        self._capture_tries = 0
        
    def _on_load_finished(self, success: bool):
        """Handle load finished event."""
        self.load_finished.emit(success)
        # Start capture attempts (even if success is False) — some pages render partial content
        self._capture_tries = 0
        QTimer.singleShot(1200, self._attempt_capture)
    
    def _attempt_capture(self):
        """Try to capture with retries to allow dynamic content to settle."""
        # Nudge lazy content by scrolling bottom->top
        try:
            self.web_view.page().runJavaScript("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
        # Extract text
        self.web_view.page().runJavaScript(
            "(function(){\n"
            "  try {\n"
            "    var t = (document.body && (document.body.innerText||document.body.textContent)) || '';\n"
            "    if (!t || t.trim().length === 0) {\n"
            "      t = (document.documentElement && document.documentElement.innerText) || '';\n"
            "    }\n"
            "    return t;\n"
            "  } catch(e){ return ''; }\n"
            "})()",
            self._on_text_extracted
        )

    def _on_text_extracted(self, text: str):
        """Handle extracted text; retry if too short, else capture screenshot and emit."""
        text_len = len(text.strip()) if text else 0
        if text_len < 40 and self._capture_tries < self.MAX_CAPTURE_TRIES - 1:
            self._capture_tries += 1
            QTimer.singleShot(self.CAPTURE_INTERVAL_MS, self._attempt_capture)
            return
        screenshot_bytes = self._grab_pixmap(self._full_capture)
        self.content_ready.emit(text or "", screenshot_bytes)

    def _grab_pixmap(self, full_page: bool) -> bytes:
        if not self.web_view:
            return b""
        orig_size = self.web_view.size()
        if full_page:
            self.web_view.resize(self.VIEWPORT_SIZE)
            QApplication.processEvents()
        else:
            # Ensure viewport capture uses the base viewport size for consistency
            if orig_size != self.BASE_VIEWPORT_SIZE:
                self.web_view.resize(self.BASE_VIEWPORT_SIZE)
                QApplication.processEvents()

        pixmap = self.web_view.grab()
        ratio = pixmap.devicePixelRatio() or 1.0
        image = pixmap.toImage()
        if ratio != 1.0:
            image = image.scaled(
                int(image.width() / ratio),
                int(image.height() / ratio),
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation,
            )

        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        screenshot_bytes = bytes(buffer.data())
        buffer.close()

        # Restore original size if we temporarily changed it
        if full_page or (orig_size != self.web_view.size()):
            self.web_view.resize(orig_size)
        return screenshot_bytes
    
    def load_url_sync(self, url: str, timeout: int = 30000, capture_full_page: bool = False) -> Tuple[bool, str, bytes]:
        """Load URL synchronously and return content.
        
        Args:
            url: URL to load
            timeout: Timeout in milliseconds
            
        Returns:
            Tuple of (success, text_content, screenshot_bytes)
        """
        if not self.web_view:
            return False, "", b""
        
        # Setup event loop for synchronous operation
        loop = QEventLoop()
        result_data = {"success": False, "text": "", "screenshot": b""}
        
        def on_load_finished(success):
            # Record load status but do not quit early; still allow capture attempts
            result_data["success"] = success
        
        def on_content_ready(text, screenshot):
            result_data["text"] = text
            result_data["screenshot"] = screenshot
            loop.quit()
        
        # Connect temporary signals
        self.load_finished.connect(on_load_finished)
        self.content_ready.connect(on_content_ready)
        
        try:
            # Start loading
            self._full_capture = capture_full_page
            self.web_view.load(QUrl(url))
            
            # Setup timeout
            timeout_timer = QTimer()
            timeout_timer.timeout.connect(loop.quit)
            timeout_timer.setSingleShot(True)
            timeout_timer.start(timeout + self.MAX_CAPTURE_TRIES * self.CAPTURE_INTERVAL_MS)
            
            # Wait for completion
            loop.exec()
            
            # Clean up
            timeout_timer.stop()
            
        finally:
            # Disconnect temporary signals
            self.load_finished.disconnect(on_load_finished)
            self.content_ready.disconnect(on_content_ready)
        
        final_success = bool(result_data["success"] or result_data["text"] or result_data["screenshot"])
        return final_success, result_data["text"], result_data["screenshot"]

    def load_html_sync(self, html: str, base_url: QUrl | None = None, timeout: int = 30000, capture_full_page: bool = True) -> Tuple[bool, str, bytes]:
        if not self.web_view:
            return False, "", b""

        loop = QEventLoop()
        result_data = {"success": False, "text": "", "screenshot": b""}

        def on_load_finished(success):
            result_data["success"] = success

        def on_content_ready(text, screenshot):
            result_data["text"] = text
            result_data["screenshot"] = screenshot
            loop.quit()

        self.load_finished.connect(on_load_finished)
        self.content_ready.connect(on_content_ready)

        try:
            self._full_capture = capture_full_page
            if capture_full_page:
                self.web_view.resize(self.VIEWPORT_SIZE)
            self.web_view.setHtml(html, base_url or QUrl())

            timeout_timer = QTimer()
            timeout_timer.timeout.connect(loop.quit)
            timeout_timer.setSingleShot(True)
            timeout_timer.start(timeout)

            loop.exec()

            timeout_timer.stop()

        finally:
            self.load_finished.disconnect(on_load_finished)
            self.content_ready.disconnect(on_content_ready)

        final_success = bool(result_data["success"] or result_data["text"] or result_data["screenshot"])
        return final_success, result_data["text"], result_data["screenshot"]
    
    def load_mhtml_file(self, mhtml_path: str) -> Tuple[bool, str, bytes]:
        """Load MHTML file and extract content.
        
        Args:
            mhtml_path: Path to MHTML file
            
        Returns:
            Tuple of (success, text_content, screenshot_bytes)
        """
        if not self.web_view:
            return False, "", b""
        
        # Convert to file URL
        file_url = QUrl.fromLocalFile(Path(mhtml_path).absolute())
        
        # Load the MHTML file
        return self.load_url_sync(file_url.toString())
    
    def get_web_view(self) -> Optional[QWebEngineView]:
        """Get the web view widget for embedding in UI."""
        return self.web_view
    
    def cleanup(self):
        """Clean up resources."""
        if self.web_view:
            self.web_view.stop()
            self.web_view.deleteLater()
            self.web_view = None


class LiveWebLoader:
    """Simplified interface for one-off web loading operations."""
    
    @staticmethod
    def load_url(url: str, timeout: int = 30000) -> Tuple[bool, str, bytes]:
        """Load URL and return content (static method for one-off use).
        
        Args:
            url: URL to load
            timeout: Timeout in milliseconds
            
        Returns:
            Tuple of (success, text_content, screenshot_bytes)
        """
        # Ensure QApplication exists
        app = QApplication.instance()
        if app is None:
            logger.error("QApplication instance required for web loading")
            return False, "", b""
        
        # Create temporary web engine
        engine = WebEngine()
        
        try:
            # Live: capture only current viewport for speed/stability
            success, text, screenshot = engine.load_url_sync(url, timeout, capture_full_page=False)
            return success, text, screenshot
        finally:
            engine.cleanup()
    
    @staticmethod
    def load_mhtml(mhtml_path: str) -> Tuple[bool, str, bytes]:
        """Load MHTML file and return content.
        
        Args:
            mhtml_path: Path to MHTML file
        
        Returns:
            Tuple of (success, text_content, screenshot_bytes)
        """
        # Ensure QApplication exists
        app = QApplication.instance()
        if app is None:
            logger.error("QApplication instance required for web loading")
            return False, "", b""
        
        # Create temporary web engine
        engine = WebEngine()

        try:
            raw_data = Path(mhtml_path).read_bytes()
        except Exception as e:
            logger.error(f"Failed to read MHTML file: {e}")
            engine.cleanup()
            return False, "", b""

        text_content, html_content, base_url = LiveWebLoader._extract_mhtml_parts(raw_data)

        try:
            if html_content:
                # MHTML: capture a taller page for full context
                success, text, screenshot = engine.load_html_sync(html_content, base_url, capture_full_page=True)
            else:
                success, text, screenshot = engine.load_mhtml_file(mhtml_path)

            if text_content and not text:
                text = text_content
            elif text and not text_content:
                text_content = text

            final_success = bool(success or text_content or screenshot)
            return final_success, text_content or text, screenshot
        finally:
            engine.cleanup()

    @staticmethod
    def _extract_mhtml_parts(raw_data: bytes) -> Tuple[str, str, QUrl | None]:
        text_result = ""
        html_result = ""
        base_url = None
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw_data)
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html_result = payload.decode(charset, errors="ignore")
                    except Exception:
                        html_result = payload.decode("latin1", errors="ignore")
                    soup = BeautifulSoup(html_result, "html.parser")
                    text_result = soup.get_text("\n", strip=True)
                    content_location = part.get("Content-Location")
                    if content_location:
                        base_url = QUrl(content_location)
                    break
        except Exception as e:
            logger.debug(f"Failed to extract text from MHTML: {e}")
        return text_result, html_result, base_url
_SHARED_PROFILE: QWebEngineProfile | None = None


class _AcceptLanguageInterceptor(QWebEngineUrlRequestInterceptor):
    """Interceptor to add Accept-Language (and optional Referer heuristics)."""
    def __init__(self, accept_language: str = "en-US,en;q=0.9"):
        super().__init__()
        self.accept_language = accept_language

    def interceptRequest(self, info):  # type: ignore[override]
        try:
            info.setHttpHeader(b"Accept-Language", self.accept_language.encode("utf-8"))
        except Exception:
            pass


def _ensure_shared_profile() -> QWebEngineProfile:
    """Create or return a persistent, shared profile for all webviews."""
    global _SHARED_PROFILE
    if _SHARED_PROFILE is not None:
        return _SHARED_PROFILE

    profile_name = "mind2web2-webview"
    profile = QWebEngineProfile(profile_name)

    # Persistent storage paths
    try:
        # Use CWD/cache_cookie as the root for cookies/cache to keep it near the project
        base = Path.cwd() / "cache_cookie"
        cache_dir = base / "http_cache"
        storage_dir = base / "storage"
        base.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        storage_dir.mkdir(parents=True, exist_ok=True)
        profile.setCachePath(str(cache_dir))
        profile.setPersistentStoragePath(str(storage_dir))
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    except Exception:
        pass

    # Realistic UA (kept from previous code), Accept-Language via interceptor
    try:
        profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    except Exception:
        pass

    try:
        profile.setUrlRequestInterceptor(_AcceptLanguageInterceptor("en-US,en;q=0.9,zh-CN;q=0.8"))
    except Exception:
        pass

    # Stealth JS injection at document creation
    try:
        script_source = """
            (function(){
              try {
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
              } catch(e) {}
              try {
                if (!navigator.languages || navigator.languages.length === 0) {
                  Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
                }
              } catch(e) {}
              try {
                if (!navigator.plugins || navigator.plugins.length === 0) {
                  Object.defineProperty(navigator, 'plugins', { get: () => ({ length: 3 }) });
                }
              } catch(e) {}
              try {
                const origGetParameter = WebGLRenderingContext && WebGLRenderingContext.prototype.getParameter;
                if (origGetParameter) {
                  WebGLRenderingContext.prototype.getParameter = function(param){
                    // UNMASKED_VENDOR_WEBGL = 0x9245, UNMASKED_RENDERER_WEBGL = 0x9246
                    if (param === 0x9245) return 'Google Inc.';
                    if (param === 0x9246) return 'ANGLE (Apple, Apple M1, OpenGL 4.1)';
                    return origGetParameter.apply(this, arguments);
                  };
                }
              } catch(e) {}
            })();
        """
        script = QWebEngineScript()
        script.setName("m2w2-stealth")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(script_source)
        profile.scripts().insert(script)
    except Exception:
        pass

    _SHARED_PROFILE = profile
    return profile
