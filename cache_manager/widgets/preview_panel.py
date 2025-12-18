"""Preview panel for displaying URL content in text, screenshot, or live modes."""

from __future__ import annotations
import logging
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtCore import Qt, Signal, QUrl, QSize, QBuffer, QIODevice
from PySide6.QtGui import QPixmap, QFont, QImage, QPainter, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QScrollArea,
    QPushButton, QFrame, QSizePolicy, QStackedWidget,
    QMessageBox, QButtonGroup, QFileDialog, QApplication
)
from PySide6.QtWebEngineWidgets import QWebEngineView

from ..models import KeywordDetector
from ..utils import WebEngine, ImageProcessor
from ..utils.web_engine import LiveWebLoader

logger = logging.getLogger(__name__)


class PreviewPanel(QWidget):
    """Panel for previewing URL content with multiple view modes."""
    
    # Signals
    update_cache_requested = Signal(str, str, str, bytes)  # task_id, url, text, screenshot
    upload_mhtml_requested = Signal(str, str)  # task_id, url
    
    # View modes
    MODE_TEXT = "text"
    MODE_SCREENSHOT = "screenshot"
    MODE_LIVE = "live"
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Current state
        self.current_url: Optional[str] = None
        self.current_task_id: Optional[str] = None
        self.current_text: Optional[str] = None
        self.current_screenshot: Optional[bytes] = None
        self.current_content_type: Optional[str] = None
        self.current_mode: str = self.MODE_SCREENSHOT
        
        # Web engine for live loading
        self.web_engine: Optional[WebEngine] = None
        self.live_web_view: Optional[QWebEngineView] = None
        
        self.setup_ui()
        self.setup_connections()
        
        logger.debug("Preview panel initialized")
    
    def set_task_context(self, task_id: str):
        """Set the current task context."""
        self.current_task_id = task_id
        self._update_action_buttons()
    
    def _update_action_buttons(self):
        """Update action button states."""
        has_content = bool(self.current_task_id and self.current_url)
        self.upload_mhtml_btn.setEnabled(has_content)
        self.update_btn.setEnabled(has_content and self.current_content_type == "web")
        if hasattr(self, 'open_in_browser_btn'):
            self.open_in_browser_btn.setEnabled(has_content)
    
    def setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        
        # Header with view mode controls
        header_frame = QFrame()
        header_frame.setProperty("class", "panel-frame")
        header_layout = QVBoxLayout(header_frame)
        
        # Title and URL
        title_layout = QHBoxLayout()
        
        title_label = QLabel("Preview")
        title_label.setProperty("class", "panel-header")
        title_layout.addWidget(title_label)
        
        title_layout.addStretch()
        
        # Action buttons
        self.upload_mhtml_btn = QPushButton("Upload MHTML")
        self.upload_mhtml_btn.setEnabled(False)
        self.upload_mhtml_btn.setToolTip("Upload MHTML file")
        title_layout.addWidget(self.upload_mhtml_btn)
        
        # Update button
        self.update_btn = QPushButton("Update Cache")
        self.update_btn.setEnabled(False)
        self.update_btn.setToolTip("Update cache with current live content")
        title_layout.addWidget(self.update_btn)
        
        header_layout.addLayout(title_layout)
        
        # URL display
        self.url_label = QLabel("No URL selected")
        self.url_label.setProperty("class", "info-label")
        self.url_label.setWordWrap(True)
        header_layout.addWidget(self.url_label)
        
        # View mode controls - large buttons
        mode_layout = QHBoxLayout()
        
        self.mode_group = QButtonGroup()
        self.mode_group.setExclusive(True)
        
        self.text_btn = QPushButton("Text")
        self.text_btn.setProperty("kind", "mode")
        self.text_btn.setCheckable(True)
        self.text_btn.setChecked(False)
        self.text_btn.setMinimumSize(120, 40)
        self.mode_group.addButton(self.text_btn, 0)
        mode_layout.addWidget(self.text_btn)
        
        self.screenshot_btn = QPushButton("Screenshot")
        self.screenshot_btn.setProperty("kind", "mode")
        self.screenshot_btn.setCheckable(True)
        self.screenshot_btn.setChecked(True)
        self.screenshot_btn.setMinimumSize(120, 40)
        self.mode_group.addButton(self.screenshot_btn, 1)
        mode_layout.addWidget(self.screenshot_btn)
        
        self.live_btn = QPushButton("Live")
        self.live_btn.setProperty("kind", "mode")
        self.live_btn.setCheckable(True)
        self.live_btn.setChecked(False)
        self.live_btn.setMinimumSize(120, 40)
        self.mode_group.addButton(self.live_btn, 2)
        mode_layout.addWidget(self.live_btn)
        
        mode_layout.addStretch()
        
        # Live control buttons
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.setMaximumWidth(70)
        self.reload_btn.setToolTip("Reload page")
        self.reload_btn.setVisible(False)
        mode_layout.addWidget(self.reload_btn)

        # Open in default browser
        self.open_in_browser_btn = QPushButton("Open in Browser")
        self.open_in_browser_btn.setMaximumWidth(140)
        self.open_in_browser_btn.setToolTip("Open current URL in your default browser")
        self.open_in_browser_btn.setEnabled(False)
        mode_layout.addWidget(self.open_in_browser_btn)
        
        header_layout.addLayout(mode_layout)
        layout.addWidget(header_frame)
        
        # Content area with stacked widget
        content_frame = QFrame()
        content_frame.setProperty("class", "panel-frame")
        content_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        content_layout = QVBoxLayout(content_frame)
        
        self.content_stack = QStackedWidget()
        
        # Text view
        self.text_widget = QTextEdit()
        self.text_widget.setReadOnly(True)
        self.text_widget.setFont(QFont("Consolas", 9))
        self.text_widget.setLineWrapMode(QTextEdit.WidgetWidth)
        self.content_stack.addWidget(self.text_widget)
        
        # Screenshot view
        screenshot_widget = QWidget()
        screenshot_layout = QVBoxLayout(screenshot_widget)
        
        self.screenshot_scroll = QScrollArea()
        self.screenshot_label = QLabel()
        self.screenshot_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.screenshot_label.setStyleSheet("background-color: white;")
        self.screenshot_scroll.setWidget(self.screenshot_label)
        self.screenshot_scroll.setWidgetResizable(True)
        screenshot_layout.addWidget(self.screenshot_scroll)
        
        # Screenshot info
        self.screenshot_info_label = QLabel("No screenshot available")
        self.screenshot_info_label.setProperty("class", "info-label")
        self.screenshot_info_label.setAlignment(Qt.AlignCenter)
        screenshot_layout.addWidget(self.screenshot_info_label)
        
        self.content_stack.addWidget(screenshot_widget)
        
        # Live view container with scroll area
        live_container = QWidget()
        live_layout = QVBoxLayout(live_container)
        live_layout.setContentsMargins(0, 0, 0, 0)
        
        self.live_scroll = QScrollArea()
        self.live_scroll.setWidgetResizable(True)
        self.live_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.live_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        # Placeholder widget
        self.live_placeholder = QLabel("Live view will be loaded when selected")
        self.live_placeholder.setAlignment(Qt.AlignCenter)
        self.live_placeholder.setProperty("class", "info-label")
        self.live_scroll.setWidget(self.live_placeholder)
        
        live_layout.addWidget(self.live_scroll)
        self.content_stack.addWidget(live_container)
        
        content_layout.addWidget(self.content_stack)
        
        # Status and issue detection
        self.status_label = QLabel("Ready")
        self.status_label.setProperty("class", "info-label")
        content_layout.addWidget(self.status_label)
        
        layout.addWidget(content_frame)
        
        # Set initial view
        self.content_stack.setCurrentIndex(1)  # Screenshot view
    
    def setup_connections(self):
        """Setup signal connections."""
        # Mode buttons
        self.text_btn.toggled.connect(lambda checked: self._on_mode_changed(self.MODE_TEXT) if checked else None)
        self.screenshot_btn.toggled.connect(lambda checked: self._on_mode_changed(self.MODE_SCREENSHOT) if checked else None)
        self.live_btn.toggled.connect(lambda checked: self._on_mode_changed(self.MODE_LIVE) if checked else None)
        
        # Action buttons
        self.upload_mhtml_btn.clicked.connect(self._on_upload_mhtml_clicked)
        self.update_btn.clicked.connect(self._on_update_cache_clicked)
        self.reload_btn.clicked.connect(self._on_reload_clicked)
        self.open_in_browser_btn.clicked.connect(self._on_open_in_browser_clicked)

    def _update_mode_button_styles(self):
        """No-op: styles are driven by :checked state via stylesheet."""
        pass
    
    def load_content(self, url: str, content_type: str, text: Optional[str], 
                    data: Optional[bytes], keyword_detector: Optional[KeywordDetector] = None):
        """Load content for preview."""
        self.current_url = url
        self.current_content_type = content_type
        self.current_text = text
        self.current_screenshot = data if content_type == "web" else None
        
        # Update URL display
        self.url_label.setText(url)
        
        # Update action buttons
        self._update_action_buttons()
        
        # Enable appropriate mode buttons
        if content_type == "web":
            self.text_btn.setEnabled(True)
            self.screenshot_btn.setEnabled(True)
            self.live_btn.setEnabled(True)
        elif content_type == "pdf":
            self.text_btn.setEnabled(False)
            self.screenshot_btn.setEnabled(False)
            self.live_btn.setEnabled(False)
            self._show_pdf_content(data)
            return
        
        # Always default to Screenshot mode for a new URL
        self.current_mode = self.MODE_SCREENSHOT
        self.screenshot_btn.setChecked(True)
        self.text_btn.setChecked(False)
        self.live_btn.setChecked(False)
        self._update_mode_button_styles()
        self._update_current_view()
        
        # Detect issues if text available
        if text and keyword_detector:
            detection_result = keyword_detector.detect_issues(text)
            if detection_result.has_issues:
                issues_text = f"Found {detection_result.issue_count} issue(s): {', '.join(detection_result.matched_keywords)}"
                self.status_label.setText(issues_text)
                self.status_label.setProperty("class", "warning-label")
            else:
                self.status_label.setText("No issues detected")
                self.status_label.setProperty("class", "success-label")
        else:
            self.status_label.setText("Content loaded")
            self.status_label.setProperty("class", "info-label")
        
        # Refresh style
        self.status_label.setStyle(self.status_label.style())
        
        logger.debug(f"Loaded content for {url} (type: {content_type})")
    
    def _on_mode_changed(self, mode: str):
        """Handle view mode change."""
        if mode == self.current_mode:
            return
        
        self.current_mode = mode
        self.reload_btn.setVisible(mode == self.MODE_LIVE)
        
        # Update action buttons
        self._update_action_buttons()
        # Visually highlight active mode
        self._update_mode_button_styles()
        
        self._update_current_view()
        
        logger.debug(f"View mode changed to: {mode}")
    
    def _update_current_view(self):
        """Update the current view based on mode and content."""
        if not self.current_url:
            return
        
        if self.current_mode == self.MODE_TEXT:
            self._show_text_view()
        elif self.current_mode == self.MODE_SCREENSHOT:
            self._show_screenshot_view()
        elif self.current_mode == self.MODE_LIVE:
            self._show_live_view()
    
    def _show_text_view(self):
        """Show text content view."""
        if self.current_text:
            self.text_widget.setPlainText(self.current_text)
        else:
            self.text_widget.setPlainText("No text content available")
        
        self.content_stack.setCurrentIndex(0)
    
    def _show_screenshot_view(self):
        """Show screenshot view with proper width fitting."""
        if self.current_screenshot:
            try:
                original_width, original_height, format_name, file_size = ImageProcessor.get_image_info(self.current_screenshot)
                pixmap = ImageProcessor.bytes_to_qpixmap(self.current_screenshot)

                self.screenshot_label.setPixmap(pixmap)
                self.screenshot_label.resize(pixmap.size())
                self.screenshot_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)

                self.screenshot_scroll.setWidgetResizable(False)
                self.screenshot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
                self.screenshot_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

                info_text = f"{original_width}×{original_height} • {format_name} • {file_size // 1024}KB"
                self.screenshot_info_label.setText(info_text)

            except Exception as e:
                logger.error(f"Failed to display screenshot: {e}")
                self.screenshot_label.setText("Failed to load screenshot")
                self.screenshot_info_label.setText("Error loading image")
        else:
            self.screenshot_label.setText("No screenshot available")
            self.screenshot_info_label.setText("No screenshot data")

        self.content_stack.setCurrentIndex(1)
    
    def _show_live_view(self):
        """Show live web view with proper sizing."""
        if not self.current_url:
            return
        
        # Initialize web view if needed
        if not self.live_web_view:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            from PySide6.QtWebEngineCore import QWebEnginePage
            from ..utils.web_engine import _ensure_shared_profile
            
            self.live_web_view = QWebEngineView()
            # Use the same shared persistent profile as capture engine
            try:
                profile = _ensure_shared_profile()
                page = QWebEnginePage(profile, self.live_web_view)
                self.live_web_view.setPage(page)
            except Exception:
                pass
            
            # Set a reasonable fixed size for the web view
            self.live_web_view.setMinimumSize(800, 600)
            self.live_web_view.resize(1200, 800)
            
            # Configure settings
            settings = self.live_web_view.settings()
            from PySide6.QtWebEngineCore import QWebEngineSettings
            settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            settings.setAttribute(QWebEngineSettings.AutoLoadImages, True)
            settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
            
            # Replace placeholder with web view
            self.live_scroll.setWidget(self.live_web_view)
        
        # Load URL in live view with UTM parameters removed (do not modify stored URL)
        browser_url = self.current_url
        try:
            from mind2web2.utils.url_tools import remove_utm_parameters
            if browser_url:
                browser_url = remove_utm_parameters(browser_url)
        except Exception:
            pass
        self.live_web_view.load(QUrl(browser_url))
        
        # Set current widget to live container
        live_container_index = 2  # Third widget in stack
        self.content_stack.setCurrentIndex(live_container_index)
        self.status_label.setText(f"Loading: {self.current_url}")
    
    def _show_pdf_content(self, pdf_data: Optional[bytes]):
        """Show PDF content (placeholder for now)."""
        if pdf_data:
            pdf_size = len(pdf_data) // 1024
            self.text_widget.setPlainText(f"PDF Content Available\n\nSize: {pdf_size}KB\n\nUse external PDF viewer to view content.")
        else:
            self.text_widget.setPlainText("No PDF content available")
        
        self.content_stack.setCurrentIndex(0)
        self.status_label.setText("PDF content loaded")
    
    def _on_upload_mhtml_clicked(self):
        """Handle MHTML upload button click."""
        if not self.current_task_id or not self.current_url:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select MHTML File",
            str(Path.home()),
            "MHTML Files (*.mhtml *.mht);;All Files (*)"
        )
        if not file_path:
            return

        self.status_label.setText("Loading MHTML…")
        QApplication.processEvents()

        success, text, screenshot = LiveWebLoader.load_mhtml(file_path)
        if not success:
            QMessageBox.critical(self, "Error", "Failed to load MHTML content.")
            self.status_label.setText("Failed to load MHTML")
            return

        if not text:
            text = f"Content unavailable for {Path(file_path).name}"
        if not screenshot:
            screenshot = self._create_placeholder_image("MHTML preview unavailable")

        self.update_cache_requested.emit(self.current_task_id, self.current_url, text, screenshot)
        self.current_text = text
        self.current_screenshot = screenshot
        self.current_content_type = "web"
        self._update_action_buttons()
        self._update_current_view()
        self.status_label.setText("Updated from MHTML")
    
    def _on_update_cache_clicked(self):
        """Capture directly from current Live view (no separate engine)."""
        if not self.current_task_id or not self.current_url:
            return
        # Require Live view to ensure human interactions are reflected
        if not self.live_web_view or self.current_mode != self.MODE_LIVE:
            QMessageBox.information(self, "Open Live First", "请先切换到 Live 视图，并完成需要的人机交互后再点击 Update Cache。")
            return

        def _emit_failure(msg: str, err: Exception | None = None):
            if err:
                logger.error(f"Failed to update cache from Live: {err}")
            self.status_label.setText("Capture failed")
            self.status_label.setProperty("class", "error-label")
            self.status_label.setStyle(self.status_label.style())
            QMessageBox.critical(self, "Error", msg)

        try:
            self.status_label.setText("Capturing from Live…")
            QApplication.processEvents()
            self.update_btn.setEnabled(False)

            # 1) Extract text via JS from current Live page
            def on_text(text_value: str):
                try:
                    text_captured = text_value or ""
                    # 2) Grab current viewport screenshot from Live
                    pixmap = self.live_web_view.grab()
                    image = pixmap.toImage()
                    # Post-process: downscale to width 1100 if wider, keep aspect ratio
                    try:
                        max_w = 1100
                        if image.width() > max_w:
                            image = image.scaled(max_w, int(image.height() * (max_w / image.width())), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    except Exception:
                        pass
                    buffer = QBuffer()
                    buffer.open(QIODevice.WriteOnly)
                    # Save as JPEG for size efficiency
                    image.save(buffer, "JPEG", quality=85)
                    screenshot_bytes = bytes(buffer.data())
                    buffer.close()

                    if not text_captured and not screenshot_bytes:
                        _emit_failure("Failed to capture live content (empty).")
                        return

                    self.update_cache_requested.emit(self.current_task_id, self.current_url, text_captured, screenshot_bytes)
                    self.current_text = text_captured
                    self.current_screenshot = screenshot_bytes
                    self.current_content_type = "web"
                    self._update_current_view()
                    self.status_label.setText("Cache updated")
                    self.status_label.setProperty("class", "success-label")
                    self.status_label.setStyle(self.status_label.style())
                except Exception as e:
                    _emit_failure("Failed to capture live content.", e)
                finally:
                    self.update_btn.setEnabled(True)

            # Run JavaScript to fetch text content from Live page
            try:
                self.live_web_view.page().runJavaScript(
                    "(document.body && (document.body.innerText||document.body.textContent)) || ''",
                    on_text,
                )
            except Exception as e:
                _emit_failure("Failed to execute JavaScript in Live page.", e)
                self.update_btn.setEnabled(True)

        except Exception as e:
            _emit_failure("Unexpected error during capture.", e)
            self.update_btn.setEnabled(True)

    def _create_placeholder_image(self, message: str) -> bytes:
        """Generate a fallback image when screenshots are unavailable."""
        width, height = 1100, 800
        image = QImage(width, height, QImage.Format_RGB32)
        image.fill(QColor("#f1f5f9"))

        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setPen(QColor("#cbd5e1"))
        painter.setBrush(QColor("#e2e8f0"))
        painter.drawRoundedRect(40, 40, width - 80, height - 80, 24, 24)

        painter.setFont(QFont("Arial", 26, QFont.Bold))
        painter.setPen(QColor("#0f172a"))
        painter.drawText(image.rect(), Qt.AlignCenter, message)

        painter.setFont(QFont("Arial", 12))
        painter.setPen(QColor("#475569"))
        painter.drawText(
            image.rect().adjusted(40, height - 80, -40, -40),
            Qt.AlignLeft | Qt.AlignVCenter,
            self.current_url or "",
        )
        painter.end()

        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "JPEG", quality=85)
        data = bytes(buffer.data())
        buffer.close()
        return data
    
    def _on_reload_clicked(self):
        """Handle reload button click."""
        if self.live_web_view and self.current_url:
            # Re-navigate to the original selected URL rather than reloading current page
            # This avoids staying on a navigated-away page after user interactions in Live view.
            self.live_web_view.load(QUrl(self.current_url))
            self.status_label.setText(f"Reloading: {self.current_url}")

    def _on_open_in_browser_clicked(self):
        """Open current URL in the system default browser."""
        if not self.current_url:
            return
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(self.current_url))
    
    def set_task_context(self, task_id: str):
        """Set the current task context."""
        self.current_task_id = task_id
    
    def clear(self):
        """Clear all content."""
        self.current_url = None
        self.current_task_id = None
        self.current_text = None
        self.current_screenshot = None
        self.current_content_type = None
        
        # Clear displays
        self.url_label.setText("No URL selected")
        self.text_widget.setPlainText("")
        self.screenshot_label.clear()
        self.screenshot_label.setText("No content")
        self.screenshot_info_label.setText("No screenshot available")
        self.status_label.setText("Ready")
        self.status_label.setProperty("class", "info-label")
        self.status_label.setStyle(self.status_label.style())
        
        # Disable update button
        self.update_btn.setEnabled(False)
        
        # Reset radio buttons
        self.screenshot_btn.setChecked(True)
        self.current_mode = self.MODE_SCREENSHOT
        self._update_mode_button_styles()
        self.reload_btn.setVisible(False)
        
        # Show screenshot view
        self.content_stack.setCurrentIndex(1)
    
    def cleanup(self):
        """Clean up resources."""
        if self.web_engine:
            self.web_engine.cleanup()
            self.web_engine = None
        
        if self.live_web_view:
            self.live_web_view.deleteLater()
            self.live_web_view = None
