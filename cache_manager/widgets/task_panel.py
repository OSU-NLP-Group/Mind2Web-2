"""Task panel for displaying and selecting tasks with rich two-line items."""

from __future__ import annotations
import logging
from typing import Optional, List

from PySide6.QtCore import Qt, Signal, QSortFilterProxyModel, QSize, QRect, QModelIndex
from PySide6.QtGui import (
    QStandardItemModel, QStandardItem, QPainter, QColor, QFont, QFontMetrics, QPen
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListView,
    QLineEdit, QPushButton, QFrame, QSizePolicy, QStyledItemDelegate, QStyleOptionViewItem
)

from ..models import CacheManager, TaskSummary

logger = logging.getLogger(__name__)

# Data roles
ROLE_TASK_ID = Qt.UserRole
ROLE_DETAIL = Qt.UserRole + 1
ROLE_HAS_ISSUES = Qt.UserRole + 2
ROLE_ISSUE_COUNT = Qt.UserRole + 3
ROLE_URL_COUNT = Qt.UserRole + 4
ROLE_SEVERITY = Qt.UserRole + 5  # "definite", "possible", or ""


class TaskItemDelegate(QStyledItemDelegate):
    """Custom delegate for two-line task items with status indicator."""

    ROW_HEIGHT = 52
    DOT_RADIUS = 5
    PADDING = 8

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw selection/hover background
        if option.state & 0x8000:  # State_Selected
            painter.fillRect(option.rect, QColor("#007acc"))
            text_color = QColor("#ffffff")
            detail_color = QColor("#cce5ff")
        elif option.state & 0x2000:  # State_MouseOver
            painter.fillRect(option.rect, QColor("#f0f7ff"))
            text_color = QColor("#1a1a2e")
            detail_color = QColor("#6c757d")
        else:
            text_color = QColor("#1a1a2e")
            detail_color = QColor("#6c757d")

        rect = option.rect
        pad = self.PADDING

        # Draw status dot on the left
        issue_count = index.data(ROLE_ISSUE_COUNT) or 0
        severity = index.data(ROLE_SEVERITY) or ""
        dot_x = rect.left() + pad + self.DOT_RADIUS
        dot_y = rect.top() + rect.height() // 2
        if issue_count > 0:
            dot_color = QColor("#dc2626") if severity == "definite" else QColor("#eab308")
        else:
            dot_color = QColor("#22c55e")  # green = clean
        painter.setBrush(dot_color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(dot_x - self.DOT_RADIUS, dot_y - self.DOT_RADIUS,
                            self.DOT_RADIUS * 2, self.DOT_RADIUS * 2)

        # Text area starts after the dot
        text_left = rect.left() + pad + self.DOT_RADIUS * 2 + pad
        text_right = rect.right() - pad

        # Line 1: task_id (bold)
        task_id = index.data(ROLE_TASK_ID) or index.data(Qt.DisplayRole) or ""
        title_font = QFont(option.font)
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize())
        painter.setFont(title_font)
        painter.setPen(text_color)
        title_rect = QRect(text_left, rect.top() + 6, text_right - text_left, 22)
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(title_font).elidedText(task_id, Qt.ElideRight, title_rect.width()))

        # Line 2: "12 URLs | 3 issues" in smaller text
        url_count = index.data(ROLE_URL_COUNT) or 0
        detail_parts = [f"{url_count} URLs"]
        if issue_count > 0:
            detail_parts.append(f"{issue_count} issues")
        detail_text = " | ".join(detail_parts)

        detail_font = QFont(option.font)
        detail_font.setPointSize(max(detail_font.pointSize() - 1, 8))
        painter.setFont(detail_font)
        painter.setPen(detail_color)
        detail_rect = QRect(text_left, rect.top() + 28, text_right - text_left, 18)

        # If issues, color the issue part differently
        if issue_count > 0 and not (option.state & 0x8000):
            # Draw "N URLs | " in gray
            prefix = f"{url_count} URLs | "
            painter.drawText(detail_rect, Qt.AlignLeft | Qt.AlignVCenter, prefix)
            prefix_width = QFontMetrics(detail_font).horizontalAdvance(prefix)
            # Draw issue count in red/yellow
            issue_color = QColor("#dc2626") if severity == "definite" else QColor("#d97706")
            painter.setPen(issue_color)
            issue_rect = QRect(text_left + prefix_width, detail_rect.top(),
                               detail_rect.width() - prefix_width, detail_rect.height())
            painter.drawText(issue_rect, Qt.AlignLeft | Qt.AlignVCenter, f"{issue_count} issues")
        else:
            painter.drawText(detail_rect, Qt.AlignLeft | Qt.AlignVCenter, detail_text)

        # Bottom separator line
        painter.setPen(QPen(QColor("#f0f0f0"), 1))
        painter.drawLine(rect.left() + pad, rect.bottom(), rect.right() - pad, rect.bottom())

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), self.ROW_HEIGHT)


class TaskListModel(QStandardItemModel):
    """Model for task list with additional data."""

    def __init__(self):
        super().__init__()
        self.task_summaries: dict[str, TaskSummary] = {}

    def add_task(self, task_summary: TaskSummary):
        """Add task to model."""
        self.task_summaries[task_summary.task_id] = task_summary

        item = QStandardItem(task_summary.task_id)
        item.setData(task_summary.task_id, ROLE_TASK_ID)
        item.setData(f"{task_summary.total_urls} URLs", ROLE_DETAIL)
        item.setData(task_summary.issue_urls > 0, ROLE_HAS_ISSUES)
        item.setData(task_summary.issue_urls, ROLE_ISSUE_COUNT)
        item.setData(task_summary.total_urls, ROLE_URL_COUNT)
        item.setData("", ROLE_SEVERITY)
        item.setToolTip(
            f"Task: {task_summary.task_id}\n"
            f"Total URLs: {task_summary.total_urls}\n"
            f"Web: {task_summary.web_urls} | PDF: {task_summary.pdf_urls}\n"
            f"Issues: {task_summary.issue_urls}\n"
            f"Path: {task_summary.cache_path}"
        )
        self.appendRow(item)

    def set_task_issue_count(self, task_id: str, count: int, severity: str = ""):
        """Update issue count and severity for a task item."""
        if task_id in self.task_summaries:
            self.task_summaries[task_id].issue_urls = count
        for row in range(self.rowCount()):
            item = self.item(row)
            if item and item.data(ROLE_TASK_ID) == task_id:
                item.setData(count, ROLE_ISSUE_COUNT)
                item.setData(count > 0, ROLE_HAS_ISSUES)
                if severity:
                    item.setData(severity, ROLE_SEVERITY)
                summary = self.task_summaries.get(task_id)
                if summary:
                    item.setToolTip(
                        f"Task: {summary.task_id}\n"
                        f"Total URLs: {summary.total_urls}\n"
                        f"Web: {summary.web_urls} | PDF: {summary.pdf_urls}\n"
                        f"Issues: {summary.issue_urls}\n"
                        f"Path: {summary.cache_path}"
                    )
                break

    def clear_tasks(self):
        """Clear all tasks."""
        self.clear()
        self.task_summaries.clear()

    def get_task_summary(self, task_id: str) -> Optional[TaskSummary]:
        """Get task summary by ID."""
        return self.task_summaries.get(task_id)


class TaskFilterProxyModel(QSortFilterProxyModel):
    """Filter model for task list."""

    def __init__(self):
        super().__init__()
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.show_issues_only = False

    def set_show_issues_only(self, show_issues: bool):
        self.show_issues_only = show_issues
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
        model = self.sourceModel()
        if not model:
            return True
        item = model.item(source_row)
        if not item:
            return True
        if self.filterRegularExpression().pattern():
            if not super().filterAcceptsRow(source_row, source_parent):
                return False
        if self.show_issues_only:
            has_issues = item.data(ROLE_HAS_ISSUES)
            if not has_issues:
                return False
        return True


class TaskPanel(QWidget):
    """Panel for task selection and filtering."""

    task_selected = Signal(str)  # task_id

    def __init__(self, parent=None):
        super().__init__(parent)

        self.task_model = TaskListModel()
        self.filter_model = TaskFilterProxyModel()
        self.filter_model.setSourceModel(self.task_model)

        self.cache_manager: Optional[CacheManager] = None
        self.current_task_id: Optional[str] = None

        self.setup_ui()
        self.setup_connections()

        logger.debug("Task panel initialized")

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Header
        header_frame = QFrame()
        header_frame.setProperty("class", "panel-frame")
        header_layout = QVBoxLayout(header_frame)

        title_label = QLabel("Tasks")
        title_label.setProperty("class", "panel-header")
        header_layout.addWidget(title_label)

        controls_layout = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search tasks...")
        controls_layout.addWidget(self.search_edit)

        self.issues_filter_btn = QPushButton("Issues")
        self.issues_filter_btn.setCheckable(True)
        self.issues_filter_btn.setProperty("kind", "toggle")
        self.issues_filter_btn.setToolTip("Show only tasks with issues")
        self.issues_filter_btn.setMaximumWidth(70)
        controls_layout.addWidget(self.issues_filter_btn)

        header_layout.addLayout(controls_layout)
        layout.addWidget(header_frame)

        # Task list with custom delegate
        list_frame = QFrame()
        list_frame.setProperty("class", "panel-frame")
        list_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        list_layout = QVBoxLayout(list_frame)

        self.task_list = QListView()
        self.task_list.setModel(self.filter_model)
        self.task_list.setItemDelegate(TaskItemDelegate(self.task_list))
        self.task_list.setAlternatingRowColors(False)  # delegate handles painting
        self.task_list.setSelectionMode(QListView.SingleSelection)
        self.task_list.setMouseTracking(True)  # enable hover state
        list_layout.addWidget(self.task_list)

        self.stats_label = QLabel("No tasks loaded")
        self.stats_label.setProperty("class", "info-label")
        self.stats_label.setAlignment(Qt.AlignCenter)
        list_layout.addWidget(self.stats_label)

        layout.addWidget(list_frame)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setMinimumWidth(200)
        self.setMaximumWidth(350)

    def setup_connections(self):
        self.search_edit.textChanged.connect(self.filter_model.setFilterWildcard)
        self.issues_filter_btn.toggled.connect(self.filter_model.set_show_issues_only)
        self.task_list.selectionModel().currentChanged.connect(self._on_selection_changed)

    def load_tasks(self, cache_manager: CacheManager):
        self.cache_manager = cache_manager
        self.task_model.clear_tasks()

        if not cache_manager:
            self._update_stats(0, 0, 0)
            return

        task_ids = sorted(cache_manager.get_task_ids())
        total_urls = 0
        for task_id in task_ids:
            summary = cache_manager.get_task_summary(task_id)
            if summary:
                summary.issue_urls = 0
                self.task_model.add_task(summary)
                total_urls += summary.total_urls

        self._update_stats(len(task_ids), total_urls, 0)

        if task_ids:
            first_index = self.filter_model.index(0, 0)
            if first_index.isValid():
                self.task_list.setCurrentIndex(first_index)

        logger.info(f"Loaded {len(task_ids)} tasks")

    def _on_selection_changed(self, current, previous):
        if not current.isValid():
            return
        task_id = self.filter_model.data(current, ROLE_TASK_ID)
        if task_id and task_id != self.current_task_id:
            self.current_task_id = task_id
            self.task_selected.emit(task_id)
            logger.debug(f"Task selected: {task_id}")

    def _update_stats(self, task_count: int, url_count: int, issue_count: int):
        if task_count == 0:
            self.stats_label.setText("No tasks loaded")
        else:
            text = f"{task_count} tasks, {url_count} URLs"
            if issue_count > 0:
                text += f", {issue_count} issues"
            self.stats_label.setText(text)

    def get_current_task_id(self) -> Optional[str]:
        return self.current_task_id

    def select_task(self, task_id: str):
        if not self.cache_manager:
            return
        for row in range(self.filter_model.rowCount()):
            index = self.filter_model.index(row, 0)
            if self.filter_model.data(index, ROLE_TASK_ID) == task_id:
                self.task_list.setCurrentIndex(index)
                break

    def refresh_current_task(self):
        if self.current_task_id and self.cache_manager:
            self.task_selected.emit(self.current_task_id)

    def clear(self):
        self.task_model.clear_tasks()
        self.current_task_id = None
        self._update_stats(0, 0, 0)

    def apply_task_issues(self, issues_map: dict[str, list]):
        """Apply computed task issue counts and update styles."""
        total_issues = 0
        for task_id, results in issues_map.items():
            count = len(results)
            total_issues += count
            # Determine worst severity for this task
            severity = ""
            for _, detection in results:
                if detection.severity == "definite":
                    severity = "definite"
                    break
                elif detection.severity == "possible":
                    severity = "possible"
            self.task_model.set_task_issue_count(task_id, count, severity)
        total_tasks = self.task_model.rowCount()
        total_urls = sum(s.total_urls for s in self.task_model.task_summaries.values())
        self._update_stats(total_tasks, total_urls, total_issues)
