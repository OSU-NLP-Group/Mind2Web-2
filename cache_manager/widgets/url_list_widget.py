"""Simplified URL list widget with emoji indicators and color coding."""

from __future__ import annotations
import logging
from typing import Optional, List
from urllib.parse import urlparse

from PySide6.QtCore import Qt, Signal, QSortFilterProxyModel, QEvent
from PySide6.QtGui import QStandardItemModel, QStandardItem, QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListView, QLineEdit, 
    QPushButton, QFrame, QSizePolicy, QMenu
)

from ..models import URLInfo, KeywordDetector

logger = logging.getLogger(__name__)


class URLListModel(QStandardItemModel):
    """Simplified model for URL list."""
    
    def __init__(self):
        super().__init__()
        self.url_infos: dict[str, URLInfo] = {}
        self.url_issues: dict[str, List[str]] = {}
    
    def add_url(self, url_info: URLInfo, keyword_detector: Optional[KeywordDetector] = None):
        """Add URL to model with simplified display."""
        self.url_infos[url_info.url] = url_info
        
        # Always show the full original URL (no truncation, no hiding query)
        full_url = url_info.url
        if url_info.content_type == "web":
            display_text = f"[Web] {full_url}"
        elif url_info.content_type == "pdf":
            display_text = f"[PDF] {full_url}"
        else:
            display_text = full_url
        
        # Create item
        item = QStandardItem(display_text)
        item.setData(url_info.url, Qt.UserRole)
        item.setData(url_info.task_id, Qt.UserRole + 1)
        item.setData(url_info.content_type, Qt.UserRole + 2)
        item.setToolTip(url_info.url)
        
        # Initial styling (no issues detected yet)
        self._apply_issue_styling(item, [])
        
        self.appendRow(item)
    
    def _clean_url_for_display(self, url: str) -> str:
        """Clean URL for display by removing protocol and www."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            path = parsed.path
            
            # Remove www. prefix
            if domain.startswith('www.'):
                domain = domain[4:]
            
            # Combine domain and path
            clean_url = domain
            if path and path != '/':
                clean_url += path
            
            # Add query params if they exist and are short
            if parsed.query and len(parsed.query) < 30:
                clean_url += f"?{parsed.query}"
            elif parsed.query:
                clean_url += "?..."
            
            # Truncate if too long
            if len(clean_url) > 60:
                clean_url = clean_url[:57] + "..."
            
            return clean_url
            
        except Exception:
            # Fallback to original URL cleaning
            if url.startswith(('http://', 'https://')):
                clean_url = url.split('//', 1)[1]
            else:
                clean_url = url
            
            if len(clean_url) > 60:
                clean_url = clean_url[:57] + "..."
            
            return clean_url
    
    def _apply_issue_styling(self, item: QStandardItem, issues: List[str], severity: str | None = None):
        """Apply two-level color coding based on severity.
        severity: 'definite' -> red, 'possible' -> orange, None/no issues -> default
        """
        if not issues:
            # No issues - normal color
            item.setForeground(QBrush(QColor("#2d3748")))  # Dark gray
            return
        if severity == "definite":
            item.setForeground(QBrush(QColor("#dc2626")))  # Red
        else:
            item.setForeground(QBrush(QColor("#eab308")))  # Yellow
    
    def update_url_issues(self, url: str, issues: List[str], severity: str | None = None):
        """Update issues for a specific URL."""
        self.url_issues[url] = issues
        
        # Find and update the item
        for row in range(self.rowCount()):
            item = self.item(row)
            if item and item.data(Qt.UserRole) == url:
                self._apply_issue_styling(item, issues, severity)
                
                # Update tooltip to include issues
                tooltip = url
                if issues:
                    tooltip += f"\n\nIssues found ({len(issues)}):\n" + "\n".join(f"• {issue}" for issue in issues[:5])
                    if len(issues) > 5:
                        tooltip += f"\n... and {len(issues) - 5} more"
                
                item.setToolTip(tooltip)
                break
    
    def clear_urls(self):
        """Clear all URLs."""
        self.clear()
        self.url_infos.clear()
        self.url_issues.clear()
    
    def get_url_info(self, url: str) -> Optional[URLInfo]:
        """Get URL info by URL."""
        return self.url_infos.get(url)


class URLFilterProxyModel(QSortFilterProxyModel):
    """Filter model for URL list."""
    
    def __init__(self):
        super().__init__()
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
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
        
        item = model.item(source_row)
        if not item:
            return True
        
        # Text filter
        if self.filterRegularExpression().pattern():
            if not super().filterAcceptsRow(source_row, source_parent):
                return False
        
        # Issues filter
        if self.show_issues_only:
            url = item.data(Qt.UserRole)
            if url not in model.url_issues or not model.url_issues[url]:
                return False
        
        # Content type filter
        if self.content_type_filter != "all":
            content_type = item.data(Qt.UserRole + 2)
            if content_type != self.content_type_filter:
                return False
        
        return True


class URLListWidget(QWidget):
    """Simplified URL list widget."""
    
    # Signals
    url_selected = Signal(str, str)  # task_id, url
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
        
        logger.debug("URL list widget initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        
        # Header
        header_frame = QFrame()
        header_frame.setProperty("class", "panel-frame")
        header_layout = QVBoxLayout(header_frame)
        
        title_label = QLabel("URLs")
        title_label.setProperty("class", "panel-header")
        header_layout.addWidget(title_label)
        
        # Search
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search URLs...")
        header_layout.addWidget(self.search_edit)
        
        # Filter buttons
        filter_layout = QHBoxLayout()
        
        # Content type filter
        content_type_label = QLabel("Show:")
        filter_layout.addWidget(content_type_label)
        
        self.all_filter_btn = QPushButton("All")
        self.all_filter_btn.setCheckable(True)
        self.all_filter_btn.setChecked(True)
        self.all_filter_btn.setToolTip("Show all content types")
        self.all_filter_btn.setMinimumWidth(50)
        self.all_filter_btn.setProperty("kind", "toggle")
        filter_layout.addWidget(self.all_filter_btn)
        
        self.web_filter_btn = QPushButton("Web")
        self.web_filter_btn.setCheckable(True)
        self.web_filter_btn.setToolTip("Show web content only")
        self.web_filter_btn.setMinimumWidth(65)
        self.web_filter_btn.setProperty("kind", "toggle")
        filter_layout.addWidget(self.web_filter_btn)
        
        self.pdf_filter_btn = QPushButton("PDF")
        self.pdf_filter_btn.setCheckable(True)
        self.pdf_filter_btn.setToolTip("Show PDF content only")
        self.pdf_filter_btn.setMinimumWidth(65)
        self.pdf_filter_btn.setProperty("kind", "toggle")
        filter_layout.addWidget(self.pdf_filter_btn)
        
        filter_layout.addWidget(QLabel("|"))  # Separator
        
        # Issues filter (independent toggle)
        self.issues_filter_btn = QPushButton("Issues Only")
        self.issues_filter_btn.setCheckable(True)
        self.issues_filter_btn.setProperty("kind", "toggle")
        self.issues_filter_btn.setToolTip("Show only URLs with issues")
        self.issues_filter_btn.setMinimumWidth(80)
        self.issues_filter_btn.setProperty("class", "warning-toggle")
        filter_layout.addWidget(self.issues_filter_btn)
        
        filter_layout.addStretch()
        
        header_layout.addLayout(filter_layout)
        layout.addWidget(header_frame)
        
        # URL list
        list_frame = QFrame()
        list_frame.setProperty("class", "panel-frame")
        list_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        list_layout = QVBoxLayout(list_frame)
        
        self.url_list = QListView()
        self.url_list.setModel(self.filter_model)
        self.url_list.setAlternatingRowColors(True)
        self.url_list.setSelectionMode(QListView.SingleSelection)
        self.url_list.setContextMenuPolicy(Qt.CustomContextMenu)
        # Never elide; allow long URLs to wrap fully
        try:
            self.url_list.setWordWrap(True)
            from PySide6.QtCore import Qt as _Qt
            self.url_list.setTextElideMode(_Qt.ElideNone)
        except Exception:
            pass
        self.url_list.installEventFilter(self)
        list_layout.addWidget(self.url_list)
        
        # Action buttons
        action_layout = QHBoxLayout()
        
        self.delete_url_btn = QPushButton("Delete")
        self.delete_url_btn.setEnabled(False)
        self.delete_url_btn.setProperty("class", "danger")
        self.delete_url_btn.setMinimumWidth(70)
        self.delete_url_btn.setToolTip("Delete selected URL")
        action_layout.addWidget(self.delete_url_btn)
        
        list_layout.addLayout(action_layout)
        
        # Statistics
        self.stats_label = QLabel("No URLs loaded")
        self.stats_label.setProperty("class", "info-label")
        self.stats_label.setAlignment(Qt.AlignCenter)
        list_layout.addWidget(self.stats_label)
        
        layout.addWidget(list_frame)
    
    def setup_connections(self):
        """Setup signal connections."""
        # Search
        self.search_edit.textChanged.connect(self.filter_model.setFilterWildcard)
        
        # Content type filters (mutually exclusive)
        self.all_filter_btn.toggled.connect(lambda checked: self._on_content_filter_toggled("all", checked))
        self.web_filter_btn.toggled.connect(lambda checked: self._on_content_filter_toggled("web", checked))
        self.pdf_filter_btn.toggled.connect(lambda checked: self._on_content_filter_toggled("pdf", checked))
        
        # Issues filter (independent toggle)
        self.issues_filter_btn.toggled.connect(self.filter_model.set_show_issues_only)
        
        # Selection
        self.url_list.selectionModel().currentChanged.connect(self._on_selection_changed)
        
        # Context menu
        self.url_list.customContextMenuRequested.connect(self._show_context_menu)
        
        # Action buttons
        self.delete_url_btn.clicked.connect(self._on_delete_url_clicked)
    
    def load_urls(self, task_id: str, url_infos: List[URLInfo], 
                  keyword_detector: Optional[KeywordDetector] = None):
        """Load URLs for a task."""
        self.current_task_id = task_id
        self.url_model.clear_urls()
        
        if not url_infos:
            self._update_stats(0, 0, 0, 0)
            return
        
        # Sort URLs alphabetically by cleaned URL (without http/https)
        def clean_url_for_sorting(url_info):
            url = url_info.url
            # Remove protocol
            url = url.replace('https://', '').replace('http://', '')
            # Remove www
            if url.startswith('www.'):
                url = url[4:]
            return url.lower()
        
        sorted_url_infos = sorted(url_infos, key=clean_url_for_sorting)
        
        # Add URLs to model
        web_count = 0
        pdf_count = 0
        issue_count = 0
        
        for url_info in sorted_url_infos:
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
        self.delete_url_btn.setEnabled(enabled)
    
    def _on_content_filter_toggled(self, filter_type: str, checked: bool):
        """Handle content type filter toggle (mutually exclusive)."""
        if not checked:
            return  # Ignore uncheck events
        
        # Uncheck other buttons
        if filter_type == "all":
            self.web_filter_btn.setChecked(False)
            self.pdf_filter_btn.setChecked(False)
        elif filter_type == "web":
            self.all_filter_btn.setChecked(False)
            self.pdf_filter_btn.setChecked(False)
        elif filter_type == "pdf":
            self.all_filter_btn.setChecked(False)
            self.web_filter_btn.setChecked(False)
        
        # Apply filter
        self.filter_model.set_content_type_filter(filter_type)
        
        # Update button styles
        self._update_content_filter_styles(filter_type)
    
    def _update_content_filter_styles(self, active_filter: str):
        """Update content filter button styles."""
        buttons = {
            "all": self.all_filter_btn,
            "web": self.web_filter_btn, 
            "pdf": self.pdf_filter_btn
        }
        
        # Visual state is driven by :checked in stylesheet; ensure checks are correct
        pass
    
    def _update_issues_filter_style(self, checked: bool):
        """Deprecated: styles now driven by :checked and kind=toggle."""
        pass
    
    def _show_context_menu(self, position):
        """Show context menu for URL list."""
        index = self.url_list.indexAt(position)
        if not index.isValid():
            return
        
        url = self.filter_model.data(index, Qt.UserRole)
        if not url:
            return
        
        menu = QMenu(self)
        
        # Copy URL action
        copy_action = menu.addAction("Copy URL")
        copy_action.triggered.connect(lambda: self._copy_url(url))
        
        # Open in browser action
        browser_action = menu.addAction("Open in Browser")
        browser_action.triggered.connect(lambda: self._open_in_browser(url))
        
        menu.addSeparator()
        
        # Live load action
        live_action = menu.addAction("Live Load")
        live_action.triggered.connect(self._on_live_load_clicked)
        
        # Upload MHTML action
        mhtml_action = menu.addAction("Upload MHTML")
        mhtml_action.triggered.connect(self._on_upload_mhtml_clicked)
        
        menu.addSeparator()
        
        # Delete action
        delete_action = menu.addAction("Delete URL")
        delete_action.triggered.connect(self._on_delete_url_clicked)
        
        menu.exec(self.url_list.mapToGlobal(position))
    
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
    
    def _on_delete_url_clicked(self):
        """Handle delete URL button click."""
        if self.current_task_id and self.current_url:
            self.url_delete_requested.emit(self.current_task_id, self.current_url)
    
    def _update_stats(self, total: int, web: int, pdf: int, issues: int):
        """Update statistics display."""
        if total == 0:
            self.stats_label.setText("No URLs loaded")
        else:
            parts = [f"{total} URLs"]
            if web > 0 and pdf > 0:
                parts.append(f"{web} web • {pdf} PDF")
            elif web > 0:
                parts.append(f"{web} web")
            elif pdf > 0:
                parts.append(f"{pdf} PDF")
            
            if issues > 0:
                parts.append(f"Issues: {issues}")

            self.stats_label.setText(" • ".join(parts))
    
    def update_url_issues(self, url: str, issues: List[str], severity: str | None = None):
        """Update issues for a specific URL."""
        self.url_model.update_url_issues(url, issues, severity)
    
    def clear(self):
        """Clear all URLs."""
        self.url_model.clear_urls()
        self.current_task_id = None
        self.current_url = None
        self._update_action_buttons(False)
        self._update_stats(0, 0, 0, 0)

    def eventFilter(self, obj, event):
        """Intercept key events for delete/backspace shortcuts."""
        if obj is self.url_list and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                if self.delete_url_btn.isEnabled():
                    self._on_delete_url_clicked()
                    return True
        return super().eventFilter(obj, event)

    def select_url(self, url: str):
        """Select the specified URL in the list if present."""
        if not url:
            return

        source_model = self.url_model
        for row in range(source_model.rowCount()):
            item = source_model.item(row)
            if item and item.data(Qt.UserRole) == url:
                source_index = source_model.indexFromItem(item)
                proxy_index = self.filter_model.mapFromSource(source_index)
                if proxy_index.isValid():
                    self.url_list.setCurrentIndex(proxy_index)
                    self.url_list.scrollTo(proxy_index)
                break
