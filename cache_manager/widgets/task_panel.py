"""Task panel for displaying and selecting tasks."""

from __future__ import annotations
import logging
from typing import Optional, List

from PySide6.QtCore import Qt, Signal, QSortFilterProxyModel
from PySide6.QtGui import QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListView,
    QLineEdit, QPushButton, QFrame, QSizePolicy
)

from ..models import CacheManager, TaskSummary

logger = logging.getLogger(__name__)


class TaskListModel(QStandardItemModel):
    """Model for task list with additional data."""
    
    def __init__(self):
        super().__init__()
        self.task_summaries: dict[str, TaskSummary] = {}
    
    def add_task(self, task_summary: TaskSummary):
        """Add task to model."""
        self.task_summaries[task_summary.task_id] = task_summary
        
        # Create display text
        display_text = f"{task_summary.task_id}"
        detail_text = f"{task_summary.total_urls} URLs"
        
        if task_summary.issue_urls > 0:
            detail_text += f" (Issues: {task_summary.issue_urls})"
        
        # Create item
        item = QStandardItem(display_text)
        item.setData(task_summary.task_id, Qt.UserRole)
        item.setToolTip(
            f"Task: {task_summary.task_id}\n"
            f"Total URLs: {task_summary.total_urls}\n"
            f"Web URLs: {task_summary.web_urls}\n"
            f"PDF URLs: {task_summary.pdf_urls}\n"
            f"Issues: {task_summary.issue_urls}\n"
            f"Path: {task_summary.cache_path}"
        )
        
        # Set item data for display and filtering
        item.setData(detail_text, Qt.UserRole + 1)
        item.setData(task_summary.issue_urls > 0, Qt.UserRole + 2)  # has_issues
        
        # Apply highlight for tasks with issues
        if task_summary.issue_urls > 0:
            item.setForeground(Qt.red)
        self.appendRow(item)

    def set_task_issue_count(self, task_id: str, count: int):
        """Update issue count and styling for a task item."""
        self.task_summaries[task_id].issue_urls = count
        # Find item and update
        for row in range(self.rowCount()):
            item = self.item(row)
            if item and item.data(Qt.UserRole) == task_id:
                detail_text = item.data(Qt.UserRole + 1) or ""
                # Rebuild detail text from summary
                summary = self.task_summaries[task_id]
                detail_text = f"{summary.total_urls} URLs"
                if count > 0:
                    detail_text += f" (Issues: {count})"
                    item.setForeground(Qt.red)
                    item.setData(True, Qt.UserRole + 2)
                else:
                    item.setForeground(Qt.black)
                    item.setData(False, Qt.UserRole + 2)
                item.setData(detail_text, Qt.UserRole + 1)
                # Update tooltip
                item.setToolTip(
                    f"Task: {summary.task_id}\n"
                    f"Total URLs: {summary.total_urls}\n"
                    f"Web URLs: {summary.web_urls}\n"
                    f"PDF URLs: {summary.pdf_urls}\n"
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
        """Set whether to show only tasks with issues."""
        self.show_issues_only = show_issues
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
            has_issues = item.data(Qt.UserRole + 2)
            if not has_issues:
                return False
        
        return True


class TaskPanel(QWidget):
    """Panel for task selection and filtering."""
    
    # Signals
    task_selected = Signal(str)  # task_id
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Models
        self.task_model = TaskListModel()
        self.filter_model = TaskFilterProxyModel()
        self.filter_model.setSourceModel(self.task_model)
        
        # Current state
        self.cache_manager: Optional[CacheManager] = None
        self.current_task_id: Optional[str] = None
        
        self.setup_ui()
        self.setup_connections()
        
        logger.debug("Task panel initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
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
        
        # Search and filter controls
        controls_layout = QHBoxLayout()
        
        # Search box
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search tasks...")
        controls_layout.addWidget(self.search_edit)
        
        # Issues filter button
        self.issues_filter_btn = QPushButton("Issues")
        self.issues_filter_btn.setCheckable(True)
        self.issues_filter_btn.setProperty("kind", "toggle")
        self.issues_filter_btn.setToolTip("Show only tasks with issues")
        self.issues_filter_btn.setMaximumWidth(70)
        controls_layout.addWidget(self.issues_filter_btn)
        
        header_layout.addLayout(controls_layout)
        layout.addWidget(header_frame)
        
        # Task list
        list_frame = QFrame()
        list_frame.setProperty("class", "panel-frame")
        list_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        list_layout = QVBoxLayout(list_frame)
        
        self.task_list = QListView()
        self.task_list.setModel(self.filter_model)
        self.task_list.setAlternatingRowColors(True)
        self.task_list.setSelectionMode(QListView.SingleSelection)
        list_layout.addWidget(self.task_list)
        
        # Statistics
        self.stats_label = QLabel("No tasks loaded")
        self.stats_label.setProperty("class", "info-label")
        self.stats_label.setAlignment(Qt.AlignCenter)
        list_layout.addWidget(self.stats_label)
        
        layout.addWidget(list_frame)
        
        # Set size policy
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setMinimumWidth(200)
        self.setMaximumWidth(300)
    
    def setup_connections(self):
        """Setup signal connections."""
        # Search functionality
        self.search_edit.textChanged.connect(self.filter_model.setFilterWildcard)
        
        # Issues filter
        self.issues_filter_btn.toggled.connect(self.filter_model.set_show_issues_only)
        
        # Selection
        self.task_list.selectionModel().currentChanged.connect(self._on_selection_changed)
    
    def load_tasks(self, cache_manager: CacheManager):
        """Load tasks from cache manager."""
        self.cache_manager = cache_manager
        self.task_model.clear_tasks()
        
        if not cache_manager:
            self._update_stats(0, 0, 0)
            return
        
        # Load task summaries
        task_ids = sorted(cache_manager.get_task_ids())
        total_urls = 0
        total_issues = 0
        
        for task_id in task_ids:
            summary = cache_manager.get_task_summary(task_id)
            if summary:
                summary.issue_urls = 0
                
                self.task_model.add_task(summary)
                total_urls += summary.total_urls
        
        self._update_stats(len(task_ids), total_urls, total_issues)
        
        # Select first task if available
        if task_ids:
            first_index = self.filter_model.index(0, 0)
            if first_index.isValid():
                self.task_list.setCurrentIndex(first_index)
        
        logger.info(f"Loaded {len(task_ids)} tasks")
    
    def _on_selection_changed(self, current, previous):
        """Handle task selection change."""
        if not current.isValid():
            return
        
        # Get task ID from model
        task_id = self.filter_model.data(current, Qt.UserRole)
        if task_id and task_id != self.current_task_id:
            self.current_task_id = task_id
            self.task_selected.emit(task_id)
            logger.debug(f"Task selected: {task_id}")
    
    def _update_filter_button_style(self, checked: bool):
        """Deprecated: styles now driven by :checked and kind=toggle."""
        pass
    
    def _update_stats(self, task_count: int, url_count: int, issue_count: int):
        """Update statistics display."""
        if task_count == 0:
            self.stats_label.setText("No tasks loaded")
        else:
            text = f"{task_count} tasks, {url_count} URLs"
            if issue_count > 0:
                text += f", Issues: {issue_count}"
            self.stats_label.setText(text)
    
    def get_current_task_id(self) -> Optional[str]:
        """Get currently selected task ID."""
        return self.current_task_id
    
    def select_task(self, task_id: str):
        """Programmatically select a task."""
        if not self.cache_manager:
            return
        
        # Find task in model
        for row in range(self.filter_model.rowCount()):
            index = self.filter_model.index(row, 0)
            if self.filter_model.data(index, Qt.UserRole) == task_id:
                self.task_list.setCurrentIndex(index)
                break
    
    def refresh_current_task(self):
        """Refresh the currently selected task."""
        if self.current_task_id and self.cache_manager:
            # Re-emit selection signal to trigger refresh
            self.task_selected.emit(self.current_task_id)
    
    def clear(self):
        """Clear all tasks."""
        self.task_model.clear_tasks()
        self.current_task_id = None
        self._update_stats(0, 0, 0)

    def apply_task_issues(self, issues_map: dict[str, list]):
        """Apply computed task issue counts and update styles."""
        total_issues = 0
        for task_id, results in issues_map.items():
            count = len(results)
            total_issues += count
            self.task_model.set_task_issue_count(task_id, count)
        # Update stats footer
        total_tasks = self.task_model.rowCount()
        total_urls = sum(s.total_urls for s in self.task_model.task_summaries.values())
        self._update_stats(total_tasks, total_urls, total_issues)
