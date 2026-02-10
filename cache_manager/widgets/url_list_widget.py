"""URL list widget with rich two-line display, color-coded status indicators."""

from __future__ import annotations
import logging
from typing import Optional, List
from urllib.parse import urlparse

from PySide6.QtCore import Qt, Signal, QSortFilterProxyModel, QEvent, QSize, QRect, QModelIndex
from PySide6.QtGui import (
    QStandardItemModel, QStandardItem, QBrush, QColor, QPainter, QFont, QFontMetrics, QPen
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListView, QLineEdit,
    QPushButton, QFrame, QSizePolicy, QMenu, QStyledItemDelegate, QStyleOptionViewItem,
    QProgressBar
)

from ..models import URLInfo, KeywordDetector

logger = logging.getLogger(__name__)

# Data roles
ROLE_URL = Qt.UserRole
ROLE_TASK_ID = Qt.UserRole + 1
ROLE_CONTENT_TYPE = Qt.UserRole + 2
ROLE_DOMAIN = Qt.UserRole + 3
ROLE_PATH = Qt.UserRole + 4
ROLE_ISSUE_COUNT = Qt.UserRole + 5
ROLE_SEVERITY = Qt.UserRole + 6  # "definite", "possible", ""
ROLE_REVIEWED = Qt.UserRole + 7  # "ok", "fixed", "skip", ""


class URLItemDelegate(QStyledItemDelegate):
    """Custom delegate for two-line URL items with left-border status indicator."""

    ROW_HEIGHT = 50
    BORDER_WIDTH = 4
    PADDING = 8

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect
        pad = self.PADDING

        # Background
        is_selected = bool(option.state & 0x8000)
        is_hover = bool(option.state & 0x2000)
        if is_selected:
            painter.fillRect(rect, QColor("#007acc"))
            title_color = QColor("#ffffff")
            detail_color = QColor("#cce5ff")
            badge_bg = QColor("#005a9e")
        elif is_hover:
            painter.fillRect(rect, QColor("#f0f7ff"))
            title_color = QColor("#1a1a2e")
            detail_color = QColor("#6c757d")
            badge_bg = QColor("#e2e8f0")
        else:
            title_color = QColor("#1a1a2e")
            detail_color = QColor("#6c757d")
            badge_bg = QColor("#f1f5f9")

        # Left border indicator
        severity = index.data(ROLE_SEVERITY) or ""
        issue_count = index.data(ROLE_ISSUE_COUNT) or 0
        reviewed = index.data(ROLE_REVIEWED) or ""
        if issue_count > 0:
            border_color = QColor("#dc2626") if severity == "definite" else QColor("#eab308")
        elif reviewed in ("ok", "fixed"):
            border_color = QColor("#22c55e")
        else:
            border_color = QColor("#e2e8f0")  # light gray for unreviewed/clean

        painter.fillRect(rect.left(), rect.top(), self.BORDER_WIDTH, rect.height(), border_color)

        # Content area
        text_left = rect.left() + self.BORDER_WIDTH + pad
        text_right = rect.right() - pad

        # Line 1: domain (bold) + [Web]/[PDF] badge
        domain = index.data(ROLE_DOMAIN) or ""
        content_type = index.data(ROLE_CONTENT_TYPE) or "web"

        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(title_color)
        title_rect = QRect(text_left, rect.top() + 5, text_right - text_left - 60, 20)
        elided_domain = QFontMetrics(title_font).elidedText(domain, Qt.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, elided_domain)

        # Badge [Web] or [PDF]
        badge_font = QFont(option.font)
        badge_font.setPointSize(max(badge_font.pointSize() - 2, 7))
        badge_font.setBold(True)
        badge_text = content_type.upper()
        badge_fm = QFontMetrics(badge_font)
        badge_w = badge_fm.horizontalAdvance(badge_text) + 10
        badge_h = 16
        badge_x = text_right - badge_w
        badge_y = rect.top() + 7
        painter.setBrush(badge_bg)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(badge_x, badge_y, badge_w, badge_h, 3, 3)
        painter.setFont(badge_font)
        painter.setPen(detail_color if not is_selected else title_color)
        painter.drawText(QRect(badge_x, badge_y, badge_w, badge_h), Qt.AlignCenter, badge_text)

        # Line 2: path in smaller gray text
        path_text = index.data(ROLE_PATH) or ""
        detail_font = QFont(option.font)
        detail_font.setPointSize(max(detail_font.pointSize() - 1, 8))
        painter.setFont(detail_font)
        painter.setPen(detail_color)
        detail_rect = QRect(text_left, rect.top() + 27, text_right - text_left, 18)
        elided_path = QFontMetrics(detail_font).elidedText(path_text, Qt.ElideMiddle, detail_rect.width())
        painter.drawText(detail_rect, Qt.AlignLeft | Qt.AlignVCenter, elided_path)

        # Reviewed checkmark
        if reviewed in ("ok", "fixed") and not is_selected:
            check_font = QFont(option.font)
            check_font.setPointSize(check_font.pointSize() + 2)
            painter.setFont(check_font)
            painter.setPen(QColor("#22c55e"))
            painter.drawText(QRect(text_right - 18, rect.top() + 25, 20, 20), Qt.AlignCenter, "\u2713")

        # Bottom separator
        painter.setPen(QPen(QColor("#f0f0f0"), 1))
        painter.drawLine(rect.left() + self.BORDER_WIDTH, rect.bottom(),
                         rect.right(), rect.bottom())

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), self.ROW_HEIGHT)


class URLListModel(QStandardItemModel):
    """Model for URL list with rich metadata."""

    def __init__(self):
        super().__init__()
        self.url_infos: dict[str, URLInfo] = {}
        self.url_issues: dict[str, List[str]] = {}

    def add_url(self, url_info: URLInfo, keyword_detector: Optional[KeywordDetector] = None):
        self.url_infos[url_info.url] = url_info

        # Parse URL for display
        domain, path_display = self._parse_url_parts(url_info.url)

        item = QStandardItem(url_info.url)  # DisplayRole = full URL (for filtering)
        item.setData(url_info.url, ROLE_URL)
        item.setData(url_info.task_id, ROLE_TASK_ID)
        item.setData(url_info.content_type, ROLE_CONTENT_TYPE)
        item.setData(domain, ROLE_DOMAIN)
        item.setData(path_display, ROLE_PATH)
        item.setData(0, ROLE_ISSUE_COUNT)
        item.setData("", ROLE_SEVERITY)
        item.setData("", ROLE_REVIEWED)
        item.setToolTip(url_info.url)

        self.appendRow(item)

    def _parse_url_parts(self, url: str) -> tuple[str, str]:
        """Parse URL into (domain, path_display)."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc or url[:50]
            if domain.startswith('www.'):
                domain = domain[4:]
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            if parsed.fragment:
                path += f"#{parsed.fragment}"
            return domain, path
        except Exception:
            return url[:40], ""

    def update_url_issues(self, url: str, issues: List[str], severity: str | None = None):
        self.url_issues[url] = issues
        for row in range(self.rowCount()):
            item = self.item(row)
            if item and item.data(ROLE_URL) == url:
                item.setData(len(issues), ROLE_ISSUE_COUNT)
                item.setData(severity or ("definite" if issues else ""), ROLE_SEVERITY)
                tooltip = url
                if issues:
                    tooltip += f"\n\nIssues ({len(issues)}):\n" + "\n".join(f"\u2022 {i}" for i in issues[:5])
                    if len(issues) > 5:
                        tooltip += f"\n... and {len(issues) - 5} more"
                item.setToolTip(tooltip)
                break

    def update_url_reviewed(self, url: str, status: str):
        """Update the reviewed status for a URL."""
        for row in range(self.rowCount()):
            item = self.item(row)
            if item and item.data(ROLE_URL) == url:
                item.setData(status, ROLE_REVIEWED)
                break

    def clear_urls(self):
        self.clear()
        self.url_infos.clear()
        self.url_issues.clear()

    def get_url_info(self, url: str) -> Optional[URLInfo]:
        return self.url_infos.get(url)


class URLFilterProxyModel(QSortFilterProxyModel):
    """Filter model for URL list."""

    def __init__(self):
        super().__init__()
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.show_issues_only = False
        self.show_unreviewed_only = False
        self.content_type_filter = "all"

    def set_show_issues_only(self, v: bool):
        self.show_issues_only = v
        self.invalidateFilter()

    def set_show_unreviewed_only(self, v: bool):
        self.show_unreviewed_only = v
        self.invalidateFilter()

    def set_content_type_filter(self, ct: str):
        self.content_type_filter = ct
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
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
            url = item.data(ROLE_URL)
            if url not in model.url_issues or not model.url_issues[url]:
                return False
        # Unreviewed filter
        if self.show_unreviewed_only:
            reviewed = item.data(ROLE_REVIEWED) or ""
            if reviewed in ("ok", "fixed", "skip"):
                return False
        # Content type filter
        if self.content_type_filter != "all":
            ct = item.data(ROLE_CONTENT_TYPE)
            if ct != self.content_type_filter:
                return False
        return True


class URLListWidget(QWidget):
    """URL list widget with rich display and review progress tracking."""

    url_selected = Signal(str, str)  # task_id, url
    url_delete_requested = Signal(str, str)
    url_mark_reviewed = Signal(str, str, str)  # task_id, url, status

    def __init__(self, parent=None):
        super().__init__(parent)

        self.url_model = URLListModel()
        self.filter_model = URLFilterProxyModel()
        self.filter_model.setSourceModel(self.url_model)

        self.current_task_id: Optional[str] = None
        self.current_url: Optional[str] = None

        self.setup_ui()
        self.setup_connections()

        logger.debug("URL list widget initialized")

    def setup_ui(self):
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

        content_type_label = QLabel("Show:")
        filter_layout.addWidget(content_type_label)

        self.all_filter_btn = QPushButton("All")
        self.all_filter_btn.setCheckable(True)
        self.all_filter_btn.setChecked(True)
        self.all_filter_btn.setMinimumWidth(50)
        self.all_filter_btn.setProperty("kind", "toggle")
        filter_layout.addWidget(self.all_filter_btn)

        self.web_filter_btn = QPushButton("Web")
        self.web_filter_btn.setCheckable(True)
        self.web_filter_btn.setMinimumWidth(55)
        self.web_filter_btn.setProperty("kind", "toggle")
        filter_layout.addWidget(self.web_filter_btn)

        self.pdf_filter_btn = QPushButton("PDF")
        self.pdf_filter_btn.setCheckable(True)
        self.pdf_filter_btn.setMinimumWidth(55)
        self.pdf_filter_btn.setProperty("kind", "toggle")
        filter_layout.addWidget(self.pdf_filter_btn)

        filter_layout.addWidget(QLabel("|"))

        self.issues_filter_btn = QPushButton("Issues")
        self.issues_filter_btn.setCheckable(True)
        self.issues_filter_btn.setProperty("kind", "toggle")
        self.issues_filter_btn.setMinimumWidth(65)
        filter_layout.addWidget(self.issues_filter_btn)

        self.unreviewed_filter_btn = QPushButton("Todo")
        self.unreviewed_filter_btn.setCheckable(True)
        self.unreviewed_filter_btn.setProperty("kind", "toggle")
        self.unreviewed_filter_btn.setToolTip("Show only unreviewed URLs")
        self.unreviewed_filter_btn.setMinimumWidth(55)
        filter_layout.addWidget(self.unreviewed_filter_btn)

        filter_layout.addStretch()

        header_layout.addLayout(filter_layout)

        # Review progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(16)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v/%m reviewed")
        self.progress_bar.setVisible(False)
        header_layout.addWidget(self.progress_bar)

        layout.addWidget(header_frame)

        # URL list with custom delegate
        list_frame = QFrame()
        list_frame.setProperty("class", "panel-frame")
        list_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        list_layout = QVBoxLayout(list_frame)

        self.url_list = QListView()
        self.url_list.setModel(self.filter_model)
        self.url_list.setItemDelegate(URLItemDelegate(self.url_list))
        self.url_list.setAlternatingRowColors(False)
        self.url_list.setSelectionMode(QListView.SingleSelection)
        self.url_list.setMouseTracking(True)
        self.url_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.url_list.installEventFilter(self)
        list_layout.addWidget(self.url_list)

        # Action buttons
        action_layout = QHBoxLayout()

        self.mark_reviewed_btn = QPushButton("Mark Reviewed")
        self.mark_reviewed_btn.setEnabled(False)
        self.mark_reviewed_btn.setToolTip("Mark current URL as reviewed (Ctrl+Enter)")
        self.mark_reviewed_btn.setMinimumWidth(110)
        action_layout.addWidget(self.mark_reviewed_btn)

        action_layout.addStretch()

        self.delete_url_btn = QPushButton("Delete")
        self.delete_url_btn.setEnabled(False)
        self.delete_url_btn.setProperty("class", "danger")
        self.delete_url_btn.setMinimumWidth(70)
        action_layout.addWidget(self.delete_url_btn)

        list_layout.addLayout(action_layout)

        # Statistics
        self.stats_label = QLabel("No URLs loaded")
        self.stats_label.setProperty("class", "info-label")
        self.stats_label.setAlignment(Qt.AlignCenter)
        list_layout.addWidget(self.stats_label)

        layout.addWidget(list_frame)

    def setup_connections(self):
        self.search_edit.textChanged.connect(self.filter_model.setFilterWildcard)

        self.all_filter_btn.toggled.connect(lambda c: self._on_content_filter_toggled("all", c))
        self.web_filter_btn.toggled.connect(lambda c: self._on_content_filter_toggled("web", c))
        self.pdf_filter_btn.toggled.connect(lambda c: self._on_content_filter_toggled("pdf", c))

        self.issues_filter_btn.toggled.connect(self.filter_model.set_show_issues_only)
        self.unreviewed_filter_btn.toggled.connect(self.filter_model.set_show_unreviewed_only)

        self.url_list.selectionModel().currentChanged.connect(self._on_selection_changed)
        self.url_list.customContextMenuRequested.connect(self._show_context_menu)

        self.delete_url_btn.clicked.connect(self._on_delete_url_clicked)
        self.mark_reviewed_btn.clicked.connect(self._on_mark_reviewed_clicked)

    def load_urls(self, task_id: str, url_infos: List[URLInfo],
                  keyword_detector: Optional[KeywordDetector] = None,
                  reviewed_map: Optional[dict] = None):
        """Load URLs for a task."""
        self.current_task_id = task_id
        self.url_model.clear_urls()

        if not url_infos:
            self._update_stats(0, 0, 0, 0)
            self.progress_bar.setVisible(False)
            return

        # Sort by domain then path
        def sort_key(ui):
            url = ui.url.replace('https://', '').replace('http://', '')
            if url.startswith('www.'):
                url = url[4:]
            return url.lower()

        sorted_infos = sorted(url_infos, key=sort_key)

        web_count = pdf_count = issue_count = reviewed_count = 0
        for url_info in sorted_infos:
            self.url_model.add_url(url_info, keyword_detector)
            if url_info.content_type == "web":
                web_count += 1
            elif url_info.content_type == "pdf":
                pdf_count += 1
            if url_info.has_issues:
                issue_count += 1
            # Apply reviewed status if available
            if reviewed_map and url_info.url in reviewed_map:
                status = reviewed_map[url_info.url]
                self.url_model.update_url_reviewed(url_info.url, status)
                if status in ("ok", "fixed", "skip"):
                    reviewed_count += 1

        self._update_stats(len(url_infos), web_count, pdf_count, issue_count)

        # Update progress bar
        total = len(url_infos)
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(reviewed_count)
        self.progress_bar.setVisible(total > 0)

        logger.info(f"Loaded {len(url_infos)} URLs for task {task_id}")

    def _on_selection_changed(self, current, previous):
        if not current.isValid():
            self._update_action_buttons(False)
            return
        url = self.filter_model.data(current, ROLE_URL)
        task_id = self.filter_model.data(current, ROLE_TASK_ID)
        if url and task_id:
            self.current_url = url
            self._update_action_buttons(True)
            self.url_selected.emit(task_id, url)

    def _update_action_buttons(self, enabled: bool):
        self.delete_url_btn.setEnabled(enabled)
        self.mark_reviewed_btn.setEnabled(enabled)

    def _on_content_filter_toggled(self, filter_type: str, checked: bool):
        if not checked:
            return
        if filter_type == "all":
            self.web_filter_btn.setChecked(False)
            self.pdf_filter_btn.setChecked(False)
        elif filter_type == "web":
            self.all_filter_btn.setChecked(False)
            self.pdf_filter_btn.setChecked(False)
        elif filter_type == "pdf":
            self.all_filter_btn.setChecked(False)
            self.web_filter_btn.setChecked(False)
        self.filter_model.set_content_type_filter(filter_type)

    def _on_mark_reviewed_clicked(self):
        if self.current_task_id and self.current_url:
            self.url_model.update_url_reviewed(self.current_url, "ok")
            self.url_mark_reviewed.emit(self.current_task_id, self.current_url, "ok")
            self._update_progress_bar()

    def _update_progress_bar(self):
        """Recount reviewed URLs and update progress bar."""
        reviewed = 0
        total = self.url_model.rowCount()
        for row in range(total):
            item = self.url_model.item(row)
            if item:
                status = item.data(ROLE_REVIEWED) or ""
                if status in ("ok", "fixed", "skip"):
                    reviewed += 1
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(reviewed)

    def _show_context_menu(self, position):
        index = self.url_list.indexAt(position)
        if not index.isValid():
            return
        url = self.filter_model.data(index, ROLE_URL)
        if not url:
            return

        menu = QMenu(self)
        copy_action = menu.addAction("Copy URL")
        copy_action.triggered.connect(lambda: self._copy_url(url))
        browser_action = menu.addAction("Open in Browser")
        browser_action.triggered.connect(lambda: self._open_in_browser(url))
        menu.addSeparator()
        mark_ok = menu.addAction("Mark as Reviewed")
        mark_ok.triggered.connect(lambda: self._mark_url(url, "ok"))
        mark_skip = menu.addAction("Mark as Skipped")
        mark_skip.triggered.connect(lambda: self._mark_url(url, "skip"))
        mark_clear = menu.addAction("Clear Review Status")
        mark_clear.triggered.connect(lambda: self._mark_url(url, ""))
        menu.addSeparator()
        delete_action = menu.addAction("Delete URL")
        delete_action.triggered.connect(self._on_delete_url_clicked)
        menu.exec(self.url_list.mapToGlobal(position))

    def _mark_url(self, url: str, status: str):
        self.url_model.update_url_reviewed(url, status)
        if self.current_task_id:
            self.url_mark_reviewed.emit(self.current_task_id, url, status)
        self._update_progress_bar()

    def _copy_url(self, url: str):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(url)

    def _open_in_browser(self, url: str):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))

    def _on_delete_url_clicked(self):
        if self.current_task_id and self.current_url:
            self.url_delete_requested.emit(self.current_task_id, self.current_url)

    def _update_stats(self, total: int, web: int, pdf: int, issues: int):
        if total == 0:
            self.stats_label.setText("No URLs loaded")
        else:
            parts = [f"{total} URLs"]
            if web > 0 and pdf > 0:
                parts.append(f"{web} web \u2022 {pdf} PDF")
            elif web > 0:
                parts.append(f"{web} web")
            elif pdf > 0:
                parts.append(f"{pdf} PDF")
            if issues > 0:
                parts.append(f"{issues} issues")
            self.stats_label.setText(" \u2022 ".join(parts))

    def update_url_issues(self, url: str, issues: List[str], severity: str | None = None):
        self.url_model.update_url_issues(url, issues, severity)

    def update_url_reviewed(self, url: str, status: str):
        """Update the reviewed status for a URL and refresh progress bar."""
        self.url_model.update_url_reviewed(url, status)
        self._update_progress_bar()

    def clear(self):
        self.url_model.clear_urls()
        self.current_task_id = None
        self.current_url = None
        self._update_action_buttons(False)
        self._update_stats(0, 0, 0, 0)
        self.progress_bar.setVisible(False)

    def eventFilter(self, obj, event):
        if obj is self.url_list and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                if self.delete_url_btn.isEnabled():
                    self._on_delete_url_clicked()
                    return True
        return super().eventFilter(obj, event)

    def select_url(self, url: str):
        if not url:
            return
        for row in range(self.url_model.rowCount()):
            item = self.url_model.item(row)
            if item and item.data(ROLE_URL) == url:
                source_index = self.url_model.indexFromItem(item)
                proxy_index = self.filter_model.mapFromSource(source_index)
                if proxy_index.isValid():
                    self.url_list.setCurrentIndex(proxy_index)
                    self.url_list.scrollTo(proxy_index)
                break
