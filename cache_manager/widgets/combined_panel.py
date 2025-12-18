"""Combined panel with tasks and URLs in a vertical layout."""

from __future__ import annotations
import logging
from typing import Optional, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QSplitter
)

from ..models import CacheManager, KeywordDetector
from .task_panel import TaskPanel
from .url_list_widget import URLListWidget

logger = logging.getLogger(__name__)


class CombinedPanel(QWidget):
    """Combined panel with tasks on top and URLs on bottom."""
    
    # Signals
    url_selected = Signal(str, str)  # task_id, url
    url_delete_requested = Signal(str, str)  # task_id, url
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Sub-components
        self.task_panel: Optional[TaskPanel] = None
        self.url_widget: Optional[URLListWidget] = None
        self.current_task_id: Optional[str] = None
        
        self.setup_ui()
        self.setup_connections()
        
        logger.debug("Combined panel initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Create horizontal splitter for tasks and URLs
        splitter = QSplitter(Qt.Horizontal)

        # Task panel (left)
        self.task_panel = TaskPanel()
        splitter.addWidget(self.task_panel)

        # URL widget (right)
        self.url_widget = URLListWidget()
        splitter.addWidget(self.url_widget)

        # Set proportions: tasks 2, URLs 4
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([300, 600])

        layout.addWidget(splitter)
    
    def setup_connections(self):
        """Setup signal connections."""
        if not all([self.task_panel, self.url_widget]):
            return
        
        # Task selection -> Load URLs
        self.task_panel.task_selected.connect(self.on_task_selected)
        
        # Forward URL signals
        self.url_widget.url_selected.connect(self.url_selected.emit)
        self.url_widget.url_delete_requested.connect(self.url_delete_requested.emit)
    
    def load_cache_manager(self, cache_manager: CacheManager):
        """Load cache manager into both panels."""
        self.task_panel.load_tasks(cache_manager)
        self.url_widget.clear()
    
    def on_task_selected(self, task_id: str):
        """Handle task selection and load URLs."""
        self.current_task_id = task_id
        # The main window will handle loading URLs via signal
        
    def load_urls_for_task(self, task_id: str, urls: List, keyword_detector: KeywordDetector):
        """Load URLs for the selected task."""
        if task_id == self.current_task_id:
            self.url_widget.load_urls(task_id, urls, keyword_detector)
    
    def update_url_issues(self, url: str, issues: List[str], severity: str | None = None):
        """Update issues for a specific URL."""
        self.url_widget.update_url_issues(url, issues, severity)
    
    def get_current_task_id(self) -> Optional[str]:
        """Get currently selected task ID."""
        return self.current_task_id
    
    def get_current_url(self) -> Optional[str]:
        """Get currently selected URL."""
        return self.url_widget.current_url if self.url_widget else None

    def clear(self):
        """Clear all content."""
        if self.task_panel:
            self.task_panel.clear()
        if self.url_widget:
            self.url_widget.clear()
        self.current_task_id = None

    def select_url(self, url: str):
        """Select a specific URL in the list."""
        if self.url_widget:
            self.url_widget.select_url(url)
