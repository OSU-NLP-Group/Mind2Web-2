"""Main window for the Cache Manager application."""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional, List

from PySide6.QtCore import Qt, QTimer, Signal, QBuffer, QIODevice
from PySide6.QtGui import QAction, QKeySequence, QImage, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QSplitter,
    QToolBar, QStatusBar, QMessageBox, QFileDialog, QProgressBar,
    QLabel, QMenuBar, QMenu, QInputDialog, QApplication
)

from ..models import CacheManager, KeywordDetector
from ..utils.web_engine import LiveWebLoader
from ..resources import get_app_stylesheet
from .combined_panel import CombinedPanel
from .preview_panel import PreviewPanel
from .dialogs import KeywordSettingsDialog

logger = logging.getLogger(__name__)


class CacheManagerMainWindow(QMainWindow):
    """Main application window with three-panel layout."""
    
    # Signals
    cache_loaded = Signal(str)  # agent_name
    url_selected = Signal(str, str)  # task_id, url
    
    def __init__(self):
        super().__init__()
        
        # Initialize models
        self.cache_manager = CacheManager()
        self.keyword_detector = KeywordDetector()
        
        # UI components
        self.combined_panel: Optional[CombinedPanel] = None
        self.preview_panel: Optional[PreviewPanel] = None
        
        # Status bar components
        self.status_label: Optional[QLabel] = None
        self.progress_bar: Optional[QProgressBar] = None
        
        self.setup_ui()
        self.setup_connections()
        self.setup_shortcuts()
        
        # Set initial status
        self.update_status("Ready - Select a cache folder to begin")
        
        logger.info("Cache Manager main window initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        self.setWindowTitle("Cache Manager v2.0")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)
        
        # Apply stylesheet
        self.setStyleSheet(get_app_stylesheet())
        
        # Setup menu bar
        self.setup_menu_bar()
        
        # Setup toolbar
        self.setup_toolbar()
        
        # Setup central widget with three panels
        self.setup_central_widget()
        
        # Setup status bar
        self.setup_status_bar()
    
    def setup_menu_bar(self):
        """Setup the menu bar."""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        open_action = QAction("&Open Cache Folder...", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.setStatusTip("Open a cache folder")
        open_action.triggered.connect(self.open_cache_folder)
        file_menu.addAction(open_action)
        
        refresh_action = QAction("&Refresh && Scan", self)
        refresh_action.setShortcut(QKeySequence.Refresh)
        refresh_action.setStatusTip("Refresh cache and scan all URLs for issues")
        refresh_action.triggered.connect(self.refresh_and_scan)
        file_menu.addAction(refresh_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.Quit)
        exit_action.setStatusTip("Exit application")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Tools menu
        tools_menu = menubar.addMenu("&Tools")
        
        keywords_action = QAction("&Keyword Settings...", self)
        keywords_action.setStatusTip("Configure keyword detection")
        keywords_action.triggered.connect(self.show_keyword_settings)
        tools_menu.addAction(keywords_action)
        
        # View menu
        view_menu = menubar.addMenu("&View")
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        
        about_action = QAction("&About", self)
        about_action.setStatusTip("About Cache Manager")
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
    
    def setup_toolbar(self):
        """Setup the toolbar."""
        toolbar = QToolBar("Main Toolbar")
        toolbar.setObjectName("main_toolbar")
        self.addToolBar(toolbar)
        
        # Open folder action
        open_action = toolbar.addAction("Open Cache Folder")
        open_action.setStatusTip("Open a cache folder")
        open_action.triggered.connect(self.open_cache_folder)
        
        # Combined refresh and scan action
        refresh_scan_action = toolbar.addAction("Refresh & Scan")
        refresh_scan_action.setStatusTip("Refresh cache and scan all URLs for issues")
        refresh_scan_action.triggered.connect(self.refresh_and_scan)
        
        toolbar.addSeparator()
        
        # Add URL action
        add_url_action = toolbar.addAction("Add URL")
        add_url_action.setStatusTip("Add new URL")
        add_url_action.triggered.connect(self.add_new_url)

        add_pdf_action = toolbar.addAction("Add PDF")
        add_pdf_action.setStatusTip("Add new PDF")
        add_pdf_action.triggered.connect(self.add_new_pdf)
    
    def setup_central_widget(self):
        """Setup the central widget with two panels."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main horizontal layout
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(4, 4, 4, 4)
        
        # Create splitter for two panels
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)
        
        # Combined panel (left) - Tasks + URLs
        self.combined_panel = CombinedPanel()
        splitter.addWidget(self.combined_panel)
        
        # Preview panel (right)
        self.preview_panel = PreviewPanel()
        splitter.addWidget(self.preview_panel)
        
        # Set splitter proportions: Combined 40%, Preview 60%
        splitter.setStretchFactor(0, 3)  # Tasks + URLs (2:4 inside)
        splitter.setStretchFactor(1, 2)  # Preview panel

        splitter.setSizes([780, 520])
    
    def setup_status_bar(self):
        """Setup the status bar."""
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        
        # Status label
        self.status_label = QLabel("Ready")
        status_bar.addWidget(self.status_label)
        
        # Progress bar (initially hidden)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        status_bar.addPermanentWidget(self.progress_bar)
        
        # Agent info label
        self.agent_info_label = QLabel("No cache loaded")
        self.agent_info_label.setStyleSheet("color: #6c757d;")
        status_bar.addPermanentWidget(self.agent_info_label)
    
    def setup_connections(self):
        """Setup signal-slot connections between panels."""
        if not all([self.combined_panel, self.preview_panel]):
            return
        
        # Combined panel connections
        self.combined_panel.url_selected.connect(self.on_url_selected)
        self.combined_panel.url_delete_requested.connect(self.on_url_delete_requested)
        
        # Task selection handling
        if hasattr(self.combined_panel, 'task_panel') and self.combined_panel.task_panel:
            self.combined_panel.task_panel.task_selected.connect(self.on_task_selected)
        
        # Preview panel operations
        self.preview_panel.update_cache_requested.connect(self.on_update_cache_requested)
        self.preview_panel.upload_mhtml_requested.connect(self.on_mhtml_upload_requested)
    
    def setup_shortcuts(self):
        """Setup keyboard shortcuts."""
        # Add common shortcuts
        pass
    
    def open_cache_folder(self):
        """Open cache folder dialog."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Cache Agent Folder",
            str(Path.home()),
            QFileDialog.ShowDirsOnly
        )
        
        if folder:
            self.load_cache_folder(folder)
    
    def load_cache_folder(self, folder_path: str):
        """Load cache from folder."""
        try:
            self.show_progress("Loading cache...")
            
            # Load cache in background
            QTimer.singleShot(10, lambda: self._do_load_cache(folder_path))
            
        except Exception as e:
            self.hide_progress()
            QMessageBox.critical(self, "Error", f"Failed to load cache: {str(e)}")
            logger.error(f"Failed to load cache from {folder_path}: {e}")
    
    def _do_load_cache(self, folder_path: str):
        """Actually load the cache (called via timer for UI responsiveness)."""
        try:
            successful, total = self.cache_manager.load_agent_cache(folder_path)
            
            if successful == 0:
                QMessageBox.warning(self, "Warning", "No valid tasks found in the selected folder.")
                self.hide_progress()
                return
            
            # Update UI
            agent_name = self.cache_manager.agent_name
            stats = self.cache_manager.get_statistics()
            
            # Update panels
            self.combined_panel.load_cache_manager(self.cache_manager)
            self.preview_panel.clear()

            # Compute task-level issues and update task panel highlighting
            try:
                issues_map = self.keyword_detector.scan_all_text_content(self.cache_manager)
                if hasattr(self.combined_panel, 'task_panel') and self.combined_panel.task_panel:
                    self.combined_panel.task_panel.apply_task_issues(issues_map)
            except Exception as e:
                logger.debug(f"Issue scan failed during load: {e}")
            
            # Update status
            self.agent_info_label.setText(
                f"{agent_name} | {stats['total_tasks']} tasks | {stats['total_urls']} URLs"
            )
            
            self.update_status(f"Loaded {successful}/{total} tasks from {agent_name}")
            self.hide_progress()
            
            # Emit signal
            self.cache_loaded.emit(agent_name)
            
            logger.info(f"Successfully loaded cache: {successful}/{total} tasks")
            
        except Exception as e:
            self.hide_progress()
            QMessageBox.critical(self, "Error", f"Failed to load cache: {str(e)}")
            logger.error(f"Failed to load cache: {e}")
    
    def refresh_cache(self):
        """Refresh current cache."""
        if not self.cache_manager.agent_path:
            QMessageBox.information(self, "Info", "No cache folder loaded.")
            return
        
        self.load_cache_folder(str(self.cache_manager.agent_path))
    
    def refresh_and_scan(self):
        """Refresh cache and scan all URLs for issues."""
        if not self.cache_manager.agent_path:
            QMessageBox.information(self, "Info", "No cache folder loaded.")
            return
        
        # First refresh the cache
        self.show_progress("Refreshing cache...")
        self.load_cache_folder(str(self.cache_manager.agent_path))
        
        # Then scan after a short delay to let the refresh complete
        QTimer.singleShot(500, self._do_scan_after_refresh)
    
    def _do_scan_after_refresh(self):
        """Scan URLs after refresh is complete."""
        self.show_progress("Scanning URLs for issues...")
        QTimer.singleShot(10, self._do_scan_urls)
    
    def on_task_selected(self, task_id: str):
        """Handle task selection."""
        logger.debug(f"Task selected: {task_id}")
        
        # Update URL list in combined panel
        urls = self.cache_manager.get_task_urls(task_id)
        self.combined_panel.load_urls_for_task(task_id, urls, self.keyword_detector)
        
        # Clear preview
        self.preview_panel.clear()
        
        # Auto-scan URLs for issues
        self._auto_scan_task_urls(task_id, urls)
        
        self.update_status(f"Loaded {len(urls)} URLs for task: {task_id}")
    
    def _auto_scan_task_urls(self, task_id: str, urls: List):
        """Automatically scan URLs for issues when task is loaded."""
        try:
            cache = self.cache_manager.get_task_cache(task_id)
            if not cache:
                return
            
            for url_info in urls:
                if url_info.content_type == "web":
                    try:
                        text, _ = cache.get_web(url_info.url,get_screenshot=False)
                        if text:
                            detection_result = self.keyword_detector.detect_issues(text)
                            issues = detection_result.matched_keywords + detection_result.matched_patterns
                            if issues:
                                self.combined_panel.update_url_issues(url_info.url, issues, detection_result.severity)
                    except Exception as e:
                        logger.debug(f"Failed to scan URL {url_info.url}: {e}")
                        
        except Exception as e:
            logger.error(f"Auto-scan failed for task {task_id}: {e}")
    
    def on_url_selected(self, task_id: str, url: str):
        """Handle URL selection."""
        logger.debug(f"URL selected: {task_id} -> {url}")
        
        # Get URL content
        text, data = self.cache_manager.get_url_content(task_id, url)
        
        # Update preview panel
        cache = self.cache_manager.get_task_cache(task_id)
        if cache:
            content_type = cache.has(url)
            self.preview_panel.set_task_context(task_id)
            self.preview_panel.load_content(url, content_type, text, data, self.keyword_detector)
        
        # Emit signal
        self.url_selected.emit(task_id, url)
        
        self.update_status(f"Viewing: {url}")
    
    def on_mhtml_upload_requested(self, task_id: str, url: str):
        """Legacy hook for MHTML uploads (handled in preview panel)."""
        logger.debug(f"MHTML upload requested (legacy): {task_id} -> {url}")
    
    def on_url_delete_requested(self, task_id: str, url: str):
        """Handle URL deletion request without confirmation dialog."""
        if self.cache_manager.delete_url(task_id, url):
            urls = self.cache_manager.get_task_urls(task_id)
            self.combined_panel.load_urls_for_task(task_id, urls, self.keyword_detector)

            # Clear preview if this URL was selected
            if self.preview_panel:
                self.preview_panel.clear()

            self.update_status(f"Deleted URL: {url}")
        else:
            QMessageBox.critical(self, "Error", "Failed to delete URL.")
    
    def on_update_cache_requested(self, task_id: str, url: str, text: str, screenshot: bytes):
        """Handle cache update request from preview panel."""
        if self.cache_manager.update_url_content(task_id, url, text, screenshot):
            self.update_status("Cache updated successfully")
            # Incremental update: rescan only this URL to update issues/highlight
            try:
                self._scan_single_url(task_id, url)
            except Exception as e:
                logger.debug(f"Incremental rescan failed for {url}: {e}")
        else:
            QMessageBox.critical(self, "Error", "Failed to update cache.")
    
    def add_new_url(self):
        """Add a placeholder URL entry for the selected task."""
        if not self.combined_panel:
            return

        task_id = self.combined_panel.get_current_task_id()
        if not task_id:
            QMessageBox.warning(self, "Select Task", "Please select a task before adding a URL.")
            return

        url, ok = QInputDialog.getText(
            self,
            "Add URL",
            "Enter URL to add:",
        )
        if not ok:
            return

        url = url.strip()
        if not url:
            return

        # Basic normalization to ensure protocol
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        cache = self.cache_manager.get_task_cache(task_id)
        if cache and cache.has(url):
            QMessageBox.information(self, "URL Exists", "This URL already exists in the selected task.")
            return

        placeholder_text = (
            f"Placeholder content for {url}\n\n"
            "This is a synthetic entry created via Cache Manager. "
            "Replace this text with real content once it is available."
        )

        placeholder_image = self._create_placeholder_screenshot(url)

        # Allow optional MHTML upload to populate real content
        used_mhtml = False
        mhtml_path, _ = QFileDialog.getOpenFileName(
            self,
            "Optional: Select MHTML File",
            str(Path.home()),
            "MHTML Files (*.mhtml *.mht);;All Files (*)"
        )

        if mhtml_path:
            self.update_status("Processing MHTML...")
            QApplication.processEvents()
            success, text, screenshot = LiveWebLoader.load_mhtml(mhtml_path)
            if success:
                if text:
                    placeholder_text = text
                if screenshot:
                    placeholder_image = screenshot
                used_mhtml = True
            else:
                QMessageBox.warning(
                    self,
                    "MHTML Failed",
                    "Failed to process the selected MHTML. Using placeholder content instead."
                )

        if not self.cache_manager.add_url_to_task(task_id, url, text=placeholder_text, screenshot=placeholder_image):
            QMessageBox.critical(self, "Error", "Failed to add URL to cache.")
            return

        urls = self.cache_manager.get_task_urls(task_id)
        self.combined_panel.load_urls_for_task(task_id, urls, self.keyword_detector)
        self.combined_panel.select_url(url)
        if used_mhtml:
            self.update_status(f"Added URL from MHTML: {url}")
        else:
            self.update_status(f"Added placeholder for {url}")

    def add_new_pdf(self):
        """Add a PDF URL with associated local file."""
        if not self.combined_panel:
            return

        task_id = self.combined_panel.get_current_task_id()
        if not task_id:
            QMessageBox.warning(self, "Select Task", "Please select a task before adding a PDF.")
            return

        url, ok = QInputDialog.getText(
            self,
            "Add PDF",
            "Enter PDF URL:",
        )
        if not ok:
            return

        url = url.strip()
        if not url:
            return

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        cache = self.cache_manager.get_task_cache(task_id)
        if cache and cache.has(url):
            QMessageBox.information(self, "URL Exists", "This URL already exists in the selected task.")
            return

        pdf_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select PDF File",
            str(Path.home()),
            "PDF Files (*.pdf);;All Files (*)"
        )
        if not pdf_path:
            return

        try:
            pdf_bytes = Path(pdf_path).read_bytes()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read PDF file: {e}")
            return

        if not self.cache_manager.add_url_to_task(task_id, url, pdf_bytes=pdf_bytes):
            QMessageBox.critical(self, "Error", "Failed to add PDF to cache.")
            return

        urls = self.cache_manager.get_task_urls(task_id)
        self.combined_panel.load_urls_for_task(task_id, urls, self.keyword_detector)
        self.combined_panel.select_url(url)
        self.update_status(f"Added PDF for {url}")

    def _create_placeholder_screenshot(self, url: str) -> bytes:
        """Generate a simple placeholder screenshot image."""
        width, height = 1100, 800
        image = QImage(width, height, QImage.Format_RGB32)
        image.fill(QColor("#f8fafc"))

        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setPen(QColor("#cbd5f5"))
        painter.setBrush(QColor("#e2e8f0"))
        painter.drawRoundedRect(40, 40, width - 80, height - 80, 24, 24)

        title_font = QFont("Arial", 28, QFont.Bold)
        painter.setFont(title_font)
        painter.setPen(QColor("#1e293b"))
        painter.drawText(image.rect(), Qt.AlignCenter, "Placeholder Screenshot")

        painter.setFont(QFont("Arial", 16))
        painter.setPen(QColor("#475569"))
        painter.drawText(
            image.rect().adjusted(0, 160, 0, -40),
            Qt.AlignCenter,
            "Add real content later",
        )

        painter.setFont(QFont("Arial", 12))
        painter.setPen(QColor("#64748b"))
        painter.drawText(
            image.rect().adjusted(40, height - 80, -40, -40),
            Qt.AlignLeft | Qt.AlignVCenter,
            url,
        )
        painter.end()

        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "JPEG", quality=85)
        data = bytes(buffer.data())
        buffer.close()
        return data
    
    def scan_all_urls(self):
        """Scan all URLs for issues."""
        if not self.cache_manager.get_task_ids():
            QMessageBox.information(self, "Info", "No cache loaded.")
            return
        
        self.show_progress("Scanning URLs...")
        
        # Scan in background
        QTimer.singleShot(10, self._do_scan_urls)
    
    def _do_scan_urls(self):
        """Actually perform URL scanning."""
        try:
            results = self.keyword_detector.scan_all_text_content(self.cache_manager)
            self.hide_progress()
            
            if not results:
                QMessageBox.information(self, "Scan Results", "No issues found! ✅")
                return
            
            # Format results
            message_lines = ["Found issues in the following tasks:\n"]
            
            for task_id, task_results in results.items():
                message_lines.append(f"📁 {task_id} ({len(task_results)} URLs with issues):")
                for url, detection in task_results[:3]:  # Show first 3 URLs
                    severity_icon = "🔴" if detection.severity == "definite" else "🟡"
                    message_lines.append(f"  {severity_icon} {url[:60]}{'...' if len(url) > 60 else ''}")
                
                if len(task_results) > 3:
                    message_lines.append(f"  ... and {len(task_results) - 3} more")
                message_lines.append("")
            
            message = "\n".join(message_lines)
            QMessageBox.warning(self, "Scan Results", message)
            
        except Exception as e:
            self.hide_progress()
            QMessageBox.critical(self, "Error", f"Scan failed: {str(e)}")
    
    def _scan_single_url(self, task_id: str, url: str):
        """Scan a single URL for issues and update display."""
        try:
            cache = self.cache_manager.get_task_cache(task_id)
            if not cache:
                return
            
            content_type = cache.has(url)
            if content_type == "web":
                text, _ = cache.get_web(url, get_screenshot=False)
                if text:
                    detection_result = self.keyword_detector.detect_issues(text)
                    issues = detection_result.matched_keywords + detection_result.matched_patterns
                    # Update URL panel display
                    self.combined_panel.update_url_issues(url, issues, detection_result.severity)
                    
                    logger.debug(f"Scanned URL {url}: {len(issues)} issues found")
                    
        except Exception as e:
            logger.error(f"Failed to scan URL {url}: {e}")
    
    def show_keyword_settings(self):
        """Show keyword settings dialog."""
        dialog = KeywordSettingsDialog(self.keyword_detector, self)
        dialog.exec()
    
    def show_about(self):
        """Show about dialog."""
        QMessageBox.about(
            self,
            "About Cache Manager",
            "<h3>Cache Manager v2.0</h3>"
            "<p>A modern cross-platform cache management tool for Mind2Web2 project.</p>"
            "<p>Built with PySide6 for optimal performance and user experience.</p>"
            "<p><b>Features:</b></p>"
            "<ul>"
            "<li>Multi-task cache inspection</li>"
            "<li>Live URL loading and updating</li>"
            "<li>MHTML file processing</li>"
            "<li>Intelligent issue detection</li>"
            "<li>Cross-platform compatibility</li>"
            "</ul>"
        )
    
    def show_progress(self, message: str):
        """Show progress bar with message."""
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate progress
        self.update_status(message)
    
    def hide_progress(self):
        """Hide progress bar."""
        self.progress_bar.setVisible(False)
    
    def update_status(self, message: str):
        """Update status bar message."""
        self.status_label.setText(message)
        logger.debug(f"Status: {message}")
    
    def closeEvent(self, event):
        """Handle close event."""
        # Clean up resources
        if hasattr(self.preview_panel, 'cleanup'):
            self.preview_panel.cleanup()
        
        event.accept()
        logger.info("Cache Manager main window closed")
