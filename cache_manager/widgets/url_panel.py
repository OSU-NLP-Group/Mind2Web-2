"""URL panel for displaying and managing URLs within a task."""

from __future__ import annotations
import logging
from typing import Optional, List
from urllib.parse import urlparse

from PySide6.QtCore import Qt, Signal, QSortFilterProxyModel
from PySide6.QtGui import QStandardItemModel, QStandardItem, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeView, QLineEdit, 
    QPushButton, QFrame, QSizePolicy, QHeaderView, QMenu, QMessageBox
)

from ..models import URLInfo, KeywordDetector

logger = logging.getLogger(__name__)


class URLListModel(QStandardItemModel):
    """Model for URL list with metadata."""
    
    def __init__(self):
        super().__init__()
        self.setHorizontalHeaderLabels(["URL", "Type", "Status", "Issues"])
        self.url_infos: dict[str, URLInfo] = {}
    
    def add_url(self, url_info: URLInfo, keyword_detector: Optional[KeywordDetector] = None):
        """Add URL to model."""
        self.url_infos[url_info.url] = url_info
        
        # Create row items
        items = []
        
        # URL item (with truncated display)
        url_item = QStandardItem(self._truncate_url(url_info.url))
        url_item.setData(url_info.url, Qt.UserRole)
        url_item.setData(url_info.task_id, Qt.UserRole + 1)
        url_item.setToolTip(url_info.url)
        items.append(url_item)
        
        # Type item
        type_icon = "🌐" if url_info.content_type == "web" else "📄"
        type_item = QStandardItem(f"{type_icon} {url_info.content_type.upper()}")
        type_item.setData(url_info.content_type, Qt.UserRole)
        items.append(type_item)
        
        # Status item (will be updated based on content analysis)
        status_item = QStandardItem("OK")
        status_item.setData("ok", Qt.UserRole)
        items.append(status_item)
        
        # Issues item (will be populated if issues found)
        issues_item = QStandardItem("")
        issues_item.setData([], Qt.UserRole)  # List of issue descriptions
        items.append(issues_item)
        
        # Add to model
        self.appendRow(items)
        
        # Update issues if detector available
        if keyword_detector and url_info.content_type == "web":
            self._analyze_url_issues(url_info, keyword_detector, len(self.url_infos) - 1)
    
    def _truncate_url(self, url: str, max_length: int = 50) -> str:
        """Truncate URL for display."""
        if len(url) <= max_length:
            return url
        
        # Try to keep the domain and path structure visible
        parsed = urlparse(url)
        domain = parsed.netloc
        path = parsed.path
        
        if len(domain) + len(path) <= max_length - 3:
            return f"{parsed.scheme}://{domain}{path}..."
        elif len(domain) <= max_length - 6:
            return f"{parsed.scheme}://{domain}...{path[-10:] if path else ''}"
        else:
            return url[:max_length-3] + "..."
    
    def _analyze_url_issues(self, url_info: URLInfo, keyword_detector: KeywordDetector, row: int):
        """Analyze URL for issues using keyword detector."""
        # This would require getting the actual text content
        # For now, just mark as analyzed
        status_item = self.item(row, 2)
        if status_item:
            status_item.setText("✓")
            status_item.setData("analyzed", Qt.UserRole)
    
    def clear_urls(self):
        """Clear all URLs."""
        self.clear()
        self.setHorizontalHeaderLabels(["URL", "Type", "Status", "Issues"])
        self.url_infos.clear()
    
    def get_url_info(self, url: str) -> Optional[URLInfo]:
        """Get URL info by URL."""
        return self.url_infos.get(url)
    
    def update_url_issues(self, url: str, issues: List[str]):
        """Update issues for a specific URL."""
        # Find row for this URL
        for row in range(self.rowCount()):
            url_item = self.item(row, 0)
            if url_item and url_item.data(Qt.UserRole) == url:
                status_item = self.item(row, 2)
                issues_item = self.item(row, 3)
                
                if issues:
                    # Has issues
                    if status_item:
                        status_item.setText("⚠️")
                        status_item.setData("issues", Qt.UserRole)
                        status_item.setToolTip(f"{len(issues)} issue(s) found")
                    
                    if issues_item:
                        issues_text = f"{len(issues)} issue(s)"
                        issues_item.setText(issues_text)
                        issues_item.setData(issues, Qt.UserRole)
                        issues_item.setToolTip("\n".join(issues))
                else:
                    # No issues
                    if status_item:
                        status_item.setText("✅")
                        status_item.setData("clean", Qt.UserRole)
                        status_item.setToolTip("No issues found")
                    
                    if issues_item:
                        issues_item.setText("")
                        issues_item.setData([], Qt.UserRole)
                        issues_item.setToolTip("")
                break


class URLFilterProxyModel(QSortFilterProxyModel):
    """Filter model for URL list."""
    
    def __init__(self):
        super().__init__()
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(0)  # Filter on URL column
        self.show_issues_only = False
        self.content_type_filter = "all"  # "all", "web", "pdf"
    
    def set_show_issues_only(self, show_issues: bool):
        """Set whether to show only URLs with issues."""
        self.show_issues_only = show_issues
        self.invalidateFilter()
    
    def set_content_type_filter(self, content_type: str):
        """Set content type filter."""
        self.content_type_filter = content_type
        self.invalidateFilter()
    
    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
        """Check if row should be accepted by filter."""
        model = self.sourceModel()
        if not model:
            return True
        
        # Text filter
        if self.filterRegularExpression().pattern():
            if not super().filterAcceptsRow(source_row, source_parent):
                return False
        
        # Issues filter
        if self.show_issues_only:
            status_item = model.item(source_row, 2)
            if not status_item or status_item.data(Qt.UserRole) != "issues":
                return False
        
        # Content type filter
        if self.content_type_filter != "all":
            type_item = model.item(source_row, 1)
            if not type_item or type_item.data(Qt.UserRole) != self.content_type_filter:
                return False
        
        return True


class URLPanel(QWidget):
    """Panel for URL display and management."""
    
    # Signals
    url_selected = Signal(str, str)  # task_id, url
    live_load_requested = Signal(str, str)  # task_id, url
    mhtml_upload_requested = Signal(str, str)  # task_id, url
    url_delete_requested = Signal(str, str)  # task_id, url
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Models
        self.url_model = URLListModel()
        self.filter_model = URLFilterProxyModel()
        self.filter_model.setSourceModel(self.url_model)
        
        # Current state
        self.current_task_id: Optional[str] = None
        self.current_url: Optional[str] = None
        
        self.setup_ui()
        self.setup_connections()
        
        logger.debug("URL panel initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        
        # Header
        header_frame = QFrame()
        header_frame.setProperty("class", "panel-frame")
        header_layout = QVBoxLayout(header_frame)
        
        title_label = QLabel("URLs")
        title_label.setProperty("class", "panel-header")
        header_layout.addWidget(title_label)
        
        # Controls row 1: Search
        search_layout = QHBoxLayout()
        
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search URLs...")
        search_layout.addWidget(self.search_edit)
        
        header_layout.addLayout(search_layout)
        
        # Controls row 2: Filters and actions
        controls_layout = QHBoxLayout()
        
        # Content type filter
        self.web_filter_btn = QPushButton("🌐")
        self.web_filter_btn.setCheckable(True)
        self.web_filter_btn.setToolTip("Show web content only")
        self.web_filter_btn.setMaximumWidth(32)
        controls_layout.addWidget(self.web_filter_btn)
        
        self.pdf_filter_btn = QPushButton("📄")
        self.pdf_filter_btn.setCheckable(True)
        self.pdf_filter_btn.setToolTip("Show PDF content only")
        self.pdf_filter_btn.setMaximumWidth(32)
        controls_layout.addWidget(self.pdf_filter_btn)
        
        # Issues filter
        self.issues_filter_btn = QPushButton("⚠️")
        self.issues_filter_btn.setCheckable(True)
        self.issues_filter_btn.setProperty("kind", "toggle")
        self.issues_filter_btn.setToolTip("Show only URLs with issues")
        self.issues_filter_btn.setMaximumWidth(32)
        controls_layout.addWidget(self.issues_filter_btn)
        
        controls_layout.addStretch()
        
        # Action buttons
        self.add_url_btn = QPushButton("➕")
        self.add_url_btn.setToolTip("Add new URL")
        self.add_url_btn.setMaximumWidth(32)
        controls_layout.addWidget(self.add_url_btn)
        
        header_layout.addLayout(controls_layout)
        layout.addWidget(header_frame)
        
        # URL list
        list_frame = QFrame()
        list_frame.setProperty("class", "panel-frame")
        list_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        list_layout = QVBoxLayout(list_frame)
        
        self.url_tree = QTreeView()
        self.url_tree.setModel(self.filter_model)
        self.url_tree.setAlternatingRowColors(True)
        self.url_tree.setSelectionMode(QTreeView.SingleSelection)
        self.url_tree.setRootIsDecorated(False)
        self.url_tree.setSortingEnabled(True)
        
        # Setup columns
        header = self.url_tree.header()
        header.setStretchLastSection(False)
        header.resizeSection(0, 250)  # URL column
        header.resizeSection(1, 60)   # Type column
        header.resizeSection(2, 50)   # Status column
        header.resizeSection(3, 80)   # Issues column
        header.setDefaultSectionSize(60)
        
        # Context menu
        self.url_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        
        list_layout.addWidget(self.url_tree)
        
        # Quick action buttons
        action_layout = QHBoxLayout()
        
        self.live_load_btn = QPushButton("🔄 Live Load")
        self.live_load_btn.setEnabled(False)
        action_layout.addWidget(self.live_load_btn)
        
        self.upload_mhtml_btn = QPushButton("📁 Upload MHTML")
        self.upload_mhtml_btn.setEnabled(False)
        action_layout.addWidget(self.upload_mhtml_btn)
        
        self.delete_url_btn = QPushButton("🗑️ Delete")
        self.delete_url_btn.setEnabled(False)
        self.delete_url_btn.setProperty("class", "danger")
        action_layout.addWidget(self.delete_url_btn)
        
        list_layout.addLayout(action_layout)
        
        # Statistics
        self.stats_label = QLabel("No URLs loaded")
        self.stats_label.setProperty("class", "info-label")
        self.stats_label.setAlignment(Qt.AlignCenter)
        list_layout.addWidget(self.stats_label)
        
        layout.addWidget(list_frame)
        
        # Set size policy
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setMinimumWidth(300)
    
    def setup_connections(self):
        """Setup signal connections."""
        # Search
        self.search_edit.textChanged.connect(self.filter_model.setFilterWildcard)
        
        # Filters
        self.web_filter_btn.toggled.connect(self._on_web_filter_toggled)
        self.pdf_filter_btn.toggled.connect(self._on_pdf_filter_toggled)
        self.issues_filter_btn.toggled.connect(self.filter_model.set_show_issues_only)
        
        # Selection
        self.url_tree.selectionModel().currentChanged.connect(self._on_selection_changed)
        
        # Context menu
        self.url_tree.customContextMenuRequested.connect(self._show_context_menu)
        
        # Action buttons
        self.live_load_btn.clicked.connect(self._on_live_load_clicked)
        self.upload_mhtml_btn.clicked.connect(self._on_upload_mhtml_clicked)
        self.delete_url_btn.clicked.connect(self._on_delete_url_clicked)
        self.add_url_btn.clicked.connect(self._on_add_url_clicked)
    
    def load_urls(self, task_id: str, url_infos: List[URLInfo], 
                  keyword_detector: Optional[KeywordDetector] = None):
        """Load URLs for a task."""
        self.current_task_id = task_id
        self.url_model.clear_urls()
        
        if not url_infos:
            self._update_stats(0, 0, 0, 0)
            return
        
        # Add URLs to model
        web_count = 0
        pdf_count = 0
        issue_count = 0
        
        for url_info in url_infos:
            self.url_model.add_url(url_info, keyword_detector)
            
            if url_info.content_type == "web":
                web_count += 1
            elif url_info.content_type == "pdf":
                pdf_count += 1
            
            if url_info.has_issues:
                issue_count += 1
        
        self._update_stats(len(url_infos), web_count, pdf_count, issue_count)
        
        logger.info(f"Loaded {len(url_infos)} URLs for task {task_id}")
    
    def _on_selection_changed(self, current, previous):
        """Handle URL selection change."""
        if not current.isValid():
            self._update_action_buttons(False)
            return
        
        # Get URL and task ID from model
        url = self.filter_model.data(current, Qt.UserRole)
        task_id = self.filter_model.data(current, Qt.UserRole + 1)
        
        if url and task_id:
            self.current_url = url
            self._update_action_buttons(True)
            self.url_selected.emit(task_id, url)
            logger.debug(f"URL selected: {url}")
    
    def _update_action_buttons(self, enabled: bool):
        """Update action button states."""
        self.live_load_btn.setEnabled(enabled)
        self.upload_mhtml_btn.setEnabled(enabled)
        self.delete_url_btn.setEnabled(enabled)
    
    def _on_web_filter_toggled(self, checked: bool):
        """Handle web filter toggle."""
        if checked:
            self.pdf_filter_btn.setChecked(False)
            self.filter_model.set_content_type_filter("web")
        else:
            self.filter_model.set_content_type_filter("all")
    
    def _on_pdf_filter_toggled(self, checked: bool):
        """Handle PDF filter toggle."""
        if checked:
            self.web_filter_btn.setChecked(False)
            self.filter_model.set_content_type_filter("pdf")
        else:
            self.filter_model.set_content_type_filter("all")
    
    def _update_issues_filter_style(self, checked: bool):
        """Deprecated: styles now driven by :checked and kind=toggle."""
        pass
    
    def _show_context_menu(self, position):
        """Show context menu for URL list."""
        index = self.url_tree.indexAt(position)
        if not index.isValid():
            return
        
        url = self.filter_model.data(index, Qt.UserRole)
        if not url:
            return
        
        menu = QMenu(self)
        
        # Copy URL action
        copy_action = menu.addAction("📋 Copy URL")
        copy_action.triggered.connect(lambda: self._copy_url(url))
        
        # Open in browser action
        browser_action = menu.addAction("🌐 Open in Browser")
        browser_action.triggered.connect(lambda: self._open_in_browser(url))
        
        menu.addSeparator()
        
        # Live load action
        live_action = menu.addAction("🔄 Live Load")
        live_action.triggered.connect(self._on_live_load_clicked)
        
        # Upload MHTML action
        mhtml_action = menu.addAction("📁 Upload MHTML")
        mhtml_action.triggered.connect(self._on_upload_mhtml_clicked)
        
        menu.addSeparator()
        
        # Delete action
        delete_action = menu.addAction("🗑️ Delete URL")
        delete_action.triggered.connect(self._on_delete_url_clicked)
        
        menu.exec(self.url_tree.mapToGlobal(position))
    
    def _copy_url(self, url: str):
        """Copy URL to clipboard."""
        from PySide6.QtGui import QClipboard
        from PySide6.QtWidgets import QApplication
        
        clipboard = QApplication.clipboard()
        clipboard.setText(url)
    
    def _open_in_browser(self, url: str):
        """Open URL in default browser."""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        
        QDesktopServices.openUrl(QUrl(url))
    
    def _on_live_load_clicked(self):
        """Handle live load button click."""
        if self.current_task_id and self.current_url:
            self.live_load_requested.emit(self.current_task_id, self.current_url)
    
    def _on_upload_mhtml_clicked(self):
        """Handle upload MHTML button click."""
        if self.current_task_id and self.current_url:
            self.mhtml_upload_requested.emit(self.current_task_id, self.current_url)
    
    def _on_delete_url_clicked(self):
        """Handle delete URL button click."""
        if self.current_task_id and self.current_url:
            self.url_delete_requested.emit(self.current_task_id, self.current_url)
    
    def _on_add_url_clicked(self):
        """Handle add URL button click."""
        # This could open a dialog to add new URL
        # For now, just emit a signal or show info
        QMessageBox.information(self, "Add URL", "Add URL functionality will be implemented.")
    
    def _update_stats(self, total: int, web: int, pdf: int, issues: int):
        """Update statistics display."""
        if total == 0:
            self.stats_label.setText("No URLs loaded")
        else:
            parts = [f"{total} URLs"]
            if web > 0:
                parts.append(f"{web} web")
            if pdf > 0:
                parts.append(f"{pdf} PDF")
            if issues > 0:
                parts.append(f"⚠️ {issues} issues")
            
            self.stats_label.setText(" • ".join(parts))
    
    def update_url_issues(self, url: str, issues: List[str]):
        """Update issues for a specific URL."""
        self.url_model.update_url_issues(url, issues)
    
    def clear(self):
        """Clear all URLs."""
        self.url_model.clear_urls()
        self.current_task_id = None
        self.current_url = None
        self._update_action_buttons(False)
        self._update_stats(0, 0, 0, 0)
