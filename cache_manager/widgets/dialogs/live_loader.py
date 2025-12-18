"""Live URL loader dialog."""

from __future__ import annotations
import logging
from typing import Optional, Tuple

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QProgressBar, QTextEdit, QMessageBox, QFrame
)

from ...utils import LiveWebLoader

logger = logging.getLogger(__name__)


class LiveLoaderDialog(QDialog):
    """Dialog for loading URLs live and capturing content."""
    
    # Signals
    loading_started = Signal(str)  # url
    loading_finished = Signal(bool, str, bytes)  # success, text, screenshot
    
    def __init__(self, parent=None, initial_url: str = ""):
        super().__init__(parent)
        self.initial_url = initial_url
        
        # Results
        self.result_text: Optional[str] = None
        self.result_screenshot: Optional[bytes] = None
        self.success = False
        
        self.setup_ui()
        self.setup_connections()
        
        if initial_url:
            self.url_input.setText(initial_url)
        
        logger.debug("Live loader dialog initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        self.setWindowTitle("Live URL Loader")
        self.setMinimumSize(500, 400)
        self.resize(600, 500)
        
        layout = QVBoxLayout(self)
        
        # URL input section
        url_frame = QFrame()
        url_frame.setProperty("class", "panel-frame")
        url_layout = QVBoxLayout(url_frame)
        
        url_layout.addWidget(QLabel("URL to Load:"))
        
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter URL (e.g., https://example.com)")
        url_layout.addWidget(self.url_input)
        
        layout.addWidget(url_frame)
        
        # Control buttons
        control_layout = QHBoxLayout()
        
        self.load_btn = QPushButton("🔄 Load URL")
        self.load_btn.setDefault(True)
        control_layout.addWidget(self.load_btn)
        
        self.clear_btn = QPushButton("Clear Results")
        control_layout.addWidget(self.clear_btn)
        
        control_layout.addStretch()
        
        layout.addLayout(control_layout)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # Status label
        self.status_label = QLabel("Enter a URL and click 'Load URL' to begin")
        self.status_label.setProperty("class", "info-label")
        layout.addWidget(self.status_label)
        
        # Results section
        results_frame = QFrame()
        results_frame.setProperty("class", "panel-frame")
        results_layout = QVBoxLayout(results_frame)
        
        results_layout.addWidget(QLabel("Captured Text Content:"))
        
        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setMaximumHeight(200)
        self.text_preview.setPlaceholderText("Captured text will appear here...")
        results_layout.addWidget(self.text_preview)
        
        # Screenshot info
        self.screenshot_info = QLabel("No screenshot captured")
        self.screenshot_info.setProperty("class", "info-label")
        results_layout.addWidget(self.screenshot_info)
        
        layout.addWidget(results_frame)
        
        # Dialog buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        button_layout.addWidget(self.cancel_btn)
        
        self.use_btn = QPushButton("Use This Content")
        self.use_btn.setEnabled(False)
        button_layout.addWidget(self.use_btn)
        
        layout.addLayout(button_layout)
    
    def setup_connections(self):
        """Setup signal connections."""
        self.load_btn.clicked.connect(self.load_url)
        self.clear_btn.clicked.connect(self.clear_results)
        self.cancel_btn.clicked.connect(self.reject)
        self.use_btn.clicked.connect(self.accept)
        
        # Connect signals
        self.loading_started.connect(self._on_loading_started)
        self.loading_finished.connect(self._on_loading_finished)
        
        # Enter key in URL input
        self.url_input.returnPressed.connect(self.load_url)
    
    def load_url(self):
        """Load the URL and capture content."""
        url = self.url_input.text().strip()
        
        if not url:
            QMessageBox.warning(self, "Invalid URL", "Please enter a URL to load.")
            return
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            self.url_input.setText(url)
        
        # Start loading
        self.loading_started.emit(url)
        
        # Use QTimer to load in background (prevents UI freezing)
        QTimer.singleShot(100, lambda: self._do_load_url(url))
    
    def _do_load_url(self, url: str):
        """Actually perform the URL loading."""
        try:
            success, text, screenshot = LiveWebLoader.load_url(url, timeout=30000)
            self.loading_finished.emit(success, text, screenshot)
            
        except Exception as e:
            logger.error(f"Failed to load URL {url}: {e}")
            self.loading_finished.emit(False, "", b"")
    
    def _on_loading_started(self, url: str):
        """Handle loading start."""
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate progress
        self.status_label.setText(f"Loading: {url}")
        self.load_btn.setEnabled(False)
        self.use_btn.setEnabled(False)
        
        logger.debug(f"Started loading URL: {url}")
    
    def _on_loading_finished(self, success: bool, text: str, screenshot: bytes):
        """Handle loading completion."""
        self.progress_bar.setVisible(False)
        self.load_btn.setEnabled(True)
        
        if success:
            self.result_text = text
            self.result_screenshot = screenshot
            self.success = True
            
            # Update UI
            self.text_preview.setPlainText(text[:2000] + ("..." if len(text) > 2000 else ""))
            
            screenshot_size = len(screenshot) // 1024 if screenshot else 0
            self.screenshot_info.setText(f"Screenshot captured: {screenshot_size}KB")
            
            self.status_label.setText("✅ URL loaded successfully!")
            self.status_label.setProperty("class", "success-label")
            
            self.use_btn.setEnabled(True)
            
            logger.info(f"Successfully loaded URL content: {len(text)} chars, {screenshot_size}KB screenshot")
            
        else:
            self.result_text = None
            self.result_screenshot = None
            self.success = False
            
            self.text_preview.clear()
            self.screenshot_info.setText("No screenshot captured")
            
            self.status_label.setText("❌ Failed to load URL")
            self.status_label.setProperty("class", "error-label")
            
            self.use_btn.setEnabled(False)
            
            logger.warning("Failed to load URL")
        
        # Refresh style
        self.status_label.setStyle(self.status_label.style())
    
    def clear_results(self):
        """Clear all results."""
        self.result_text = None
        self.result_screenshot = None
        self.success = False
        
        self.text_preview.clear()
        self.screenshot_info.setText("No screenshot captured")
        self.status_label.setText("Results cleared")
        self.status_label.setProperty("class", "info-label")
        self.status_label.setStyle(self.status_label.style())
        
        self.use_btn.setEnabled(False)
    
    def get_results(self) -> Tuple[Optional[str], Optional[bytes]]:
        """Get the captured results."""
        return self.result_text, self.result_screenshot
    
    def get_url(self) -> str:
        """Get the entered URL."""
        return self.url_input.text().strip()


# Convenience function for simple usage
def load_url_dialog(parent=None, initial_url: str = "") -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
    """Show live loader dialog and return results.
    
    Returns:
        Tuple of (url, text, screenshot) or (None, None, None) if cancelled
    """
    dialog = LiveLoaderDialog(parent, initial_url)
    
    if dialog.exec() == QDialog.Accepted:
        url = dialog.get_url()
        text, screenshot = dialog.get_results()
        return url, text, screenshot
    
    return None, None, None