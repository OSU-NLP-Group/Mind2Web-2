"""Qt stylesheets for the cache manager application."""

from typing import Dict


def get_app_stylesheet() -> str:
    """Get the main application stylesheet."""
    return """
    /* Main Window */
    QMainWindow {
        background-color: #f5f5f5;
        font-family: "Segoe UI", Arial, sans-serif;
        font-size: 10pt;
    }
    
    /* Toolbar */
    QToolBar {
        background-color: #ffffff;
        border: none;
        border-bottom: 1px solid #e0e0e0;
        spacing: 8px;
        padding: 4px 8px;
    }
    
    QToolBar QToolButton {
        padding: 6px 12px;
        margin: 2px;
        border-radius: 4px;
        background-color: #f8f9fa;
        border: 1px solid #dee2e6;
        color: #495057;
    }
    
    QToolBar QToolButton:hover {
        background-color: #e9ecef;
        border-color: #adb5bd;
    }
    
    QToolBar QToolButton:pressed {
        background-color: #dee2e6;
    }
    
    /* Splitter */
    QSplitter::handle {
        background-color: #e0e0e0;
        width: 2px;
        height: 2px;
    }
    
    QSplitter::handle:hover {
        background-color: #007acc;
    }
    
    /* Panels */
    .panel-frame {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 6px;
        padding: 8px;
        margin: 4px;
    }
    
    .panel-header {
        font-weight: bold;
        color: #343a40;
        padding: 8px 0px 4px 0px;
        border-bottom: 1px solid #e9ecef;
    }
    
    /* Lists */
    QListWidget {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 4px;
        selection-background-color: #007acc;
        selection-color: white;
        outline: none;
    }
    
    QListWidget::item {
        padding: 6px 8px;
        border-bottom: 1px solid #f8f9fa;
    }
    
    QListWidget::item:hover {
        background-color: #f8f9fa;
    }
    
    QListWidget::item:selected {
        background-color: #007acc;
        color: white;
    }
    
    /* Tree View */
    QTreeView {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 4px;
        selection-background-color: #007acc;
        selection-color: white;
        outline: none;
        gridline-color: #f0f0f0;
    }
    
    QTreeView::item {
        padding: 4px 8px;
        border-bottom: 1px solid #f8f9fa;
    }
    
    QTreeView::item:hover {
        background-color: #f8f9fa;
    }
    
    QTreeView::item:selected {
        background-color: #007acc;
        color: white;
    }
    
    QTreeView QHeaderView::section {
        background-color: #f8f9fa;
        border: 1px solid #e0e0e0;
        padding: 6px 8px;
        font-weight: bold;
        color: #495057;
    }
    
    /* Buttons */
    QPushButton {
        background-color: #007acc;
        color: white;
        border: none;
        border-radius: 4px;
        padding: 8px 16px;
        font-weight: 500;
    }
    
    QPushButton:hover {
        background-color: #005a9e;
    }
    
    QPushButton:pressed {
        background-color: #004578;
    }
    
    QPushButton:disabled {
        background-color: #6c757d;
        color: #adb5bd;
    }
    
    /* Secondary Button */
    QPushButton.secondary {
        background-color: #6c757d;
        color: white;
        border: 2px solid #5a6268;
        font-weight: bold;
    }
    
    QPushButton.secondary:hover {
        background-color: #5a6268;
        border-color: #495057;
    }
    
    /* Danger Button */
    QPushButton.danger {
        background-color: #dc3545;
        color: white;
        border: 2px solid #c82333;
        font-weight: bold;
    }
    
    QPushButton.danger:hover {
        background-color: #c82333;
        border-color: #bd2130;
    }

    /* Mode Toggle Buttons (Text / Screenshot / Live) using dynamic property and :checked */
    QPushButton[kind="mode"] {
        background-color: #f1f5f9;   /* slate-100 */
        color: #334155;               /* slate-700 */
        border: 1px solid #cbd5e1;    /* slate-300 */
        border-radius: 6px;
        padding: 8px 14px;
        font-weight: 600;
    }

    QPushButton[kind="mode"]:hover {
        background-color: #e2e8f0;    /* slate-200 */
        border-color: #94a3b8;        /* slate-400 */
    }

    QPushButton[kind="mode"]:checked {
        background-color: #2563eb;    /* blue-600 */
        color: #ffffff;
        border: 1px solid #1d4ed8;    /* blue-700 */
        box-shadow: 0 1px 0 rgba(0,0,0,0.05);
    }

    QPushButton[kind="mode"]:checked:hover {
        background-color: #1d4ed8;    /* blue-700 */
        border-color: #1e40af;        /* blue-800 */
    }

    /* Generic toggle buttons (e.g., All/Web/PDF) using dynamic property and :checked */
    QPushButton[kind="toggle"] {
        background-color: #f8fafc;    /* slate-50 */
        color: #334155;               /* slate-700 */
        border: 1px solid #e2e8f0;    /* slate-200 */
        border-radius: 6px;
        padding: 6px 12px;
        font-weight: 600;
    }

    QPushButton[kind="toggle"]:hover {
        background-color: #eef2f7;
        border-color: #cbd5e1;        /* slate-300 */
    }

    QPushButton[kind="toggle"]:checked {
        background-color: #0ea5e9;    /* sky-500 */
        color: #ffffff;
        border: 1px solid #0284c7;    /* sky-600 */
    }

    QPushButton[kind="toggle"]:checked:hover {
        background-color: #0284c7;    /* sky-600 */
        border-color: #0369a1;        /* sky-700 */
    }
    
    /* Text Edit */
    QTextEdit {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 4px;
        padding: 8px;
        font-family: "Consolas", "Monaco", "Courier New", monospace;
        font-size: 9pt;
        line-height: 1.4;
    }
    
    /* Scroll Area */
    QScrollArea {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 4px;
    }
    
    /* Scroll Bars */
    QScrollBar:vertical {
        border: none;
        background-color: #f8f9fa;
        width: 12px;
        border-radius: 6px;
    }
    
    QScrollBar::handle:vertical {
        background-color: #adb5bd;
        border-radius: 6px;
        min-height: 20px;
        margin: 0px;
    }
    
    QScrollBar::handle:vertical:hover {
        background-color: #6c757d;
    }
    
    QScrollBar:horizontal {
        border: none;
        background-color: #f8f9fa;
        height: 12px;
        border-radius: 6px;
    }
    
    QScrollBar::handle:horizontal {
        background-color: #adb5bd;
        border-radius: 6px;
        min-width: 20px;
        margin: 0px;
    }
    
    QScrollBar::handle:horizontal:hover {
        background-color: #6c757d;
    }
    
    QScrollBar::add-line, QScrollBar::sub-line {
        border: none;
        background: none;
    }
    
    /* Radio Buttons */
    QRadioButton {
        spacing: 6px;
        color: #495057;
    }
    
    QRadioButton::indicator {
        width: 16px;
        height: 16px;
    }
    
    QRadioButton::indicator::unchecked {
        border: 2px solid #adb5bd;
        border-radius: 8px;
        background-color: #ffffff;
    }
    
    QRadioButton::indicator::checked {
        border: 2px solid #007acc;
        border-radius: 8px;
        background-color: #007acc;
    }
    
    QRadioButton::indicator::checked::after {
        width: 6px;
        height: 6px;
        border-radius: 3px;
        background-color: white;
        top: 3px;
        left: 3px;
    }
    
    /* Labels */
    QLabel {
        color: #495057;
    }
    
    .info-label {
        color: #6c757d;
        font-style: italic;
    }
    
    .warning-label {
        color: #fd7e14;
        font-weight: bold;
    }
    
    .error-label {
        color: #dc3545;
        font-weight: bold;
    }
    
    .success-label {
        color: #28a745;
        font-weight: bold;
    }
    
    /* Status Bar */
    QStatusBar {
        background-color: #f8f9fa;
        border-top: 1px solid #e0e0e0;
        color: #6c757d;
        padding: 4px 8px;
    }
    
    /* Tab Widget */
    QTabWidget::pane {
        border: 1px solid #e0e0e0;
        border-radius: 4px;
        background-color: #ffffff;
    }
    
    QTabBar::tab {
        background-color: #f8f9fa;
        border: 1px solid #e0e0e0;
        padding: 8px 16px;
        margin-right: 2px;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
    }
    
    QTabBar::tab:selected {
        background-color: #ffffff;
        border-bottom-color: #ffffff;
    }
    
    QTabBar::tab:hover {
        background-color: #e9ecef;
    }
    
    /* Progress Bar */
    QProgressBar {
        border: 1px solid #e0e0e0;
        border-radius: 4px;
        text-align: center;
        background-color: #f8f9fa;
    }
    
    QProgressBar::chunk {
        background-color: #007acc;
        border-radius: 3px;
    }
    """


def get_dark_stylesheet() -> str:
    """Get dark theme stylesheet."""
    return """
    /* Dark Theme */
    QMainWindow {
        background-color: #2b2b2b;
        color: #ffffff;
        font-family: "Segoe UI", Arial, sans-serif;
        font-size: 10pt;
    }
    
    QToolBar {
        background-color: #3c3c3c;
        border-bottom: 1px solid #555555;
    }
    
    QToolBar QToolButton {
        background-color: #404040;
        border: 1px solid #555555;
        color: #ffffff;
    }
    
    QToolBar QToolButton:hover {
        background-color: #4a4a4a;
        border-color: #777777;
    }
    
    .panel-frame {
        background-color: #3c3c3c;
        border: 1px solid #555555;
    }
    
    .panel-header {
        color: #ffffff;
        border-bottom: 1px solid #555555;
    }
    
    QListWidget {
        background-color: #3c3c3c;
        border: 1px solid #555555;
        color: #ffffff;
    }
    
    QListWidget::item {
        border-bottom: 1px solid #404040;
    }
    
    QListWidget::item:hover {
        background-color: #404040;
    }
    
    QTreeView {
        background-color: #3c3c3c;
        border: 1px solid #555555;
        color: #ffffff;
        gridline-color: #555555;
    }
    
    QTreeView::item:hover {
        background-color: #404040;
    }
    
    QTreeView QHeaderView::section {
        background-color: #404040;
        border: 1px solid #555555;
        color: #ffffff;
    }
    
    QPushButton {
        background-color: #0078d4;
        color: white;
    }
    
    QPushButton:hover {
        background-color: #106ebe;
    }
    
    QPushButton.secondary {
        background-color: #6c757d;
    }
    
    QTextEdit {
        background-color: #3c3c3c;
        border: 1px solid #555555;
        color: #ffffff;
    }
    
    QScrollArea {
        background-color: #3c3c3c;
        border: 1px solid #555555;
    }
    
    QScrollBar:vertical {
        background-color: #404040;
    }
    
    QScrollBar::handle:vertical {
        background-color: #666666;
    }
    
    QScrollBar:horizontal {
        background-color: #404040;
    }
    
    QScrollBar::handle:horizontal {
        background-color: #666666;
    }
    
    QRadioButton {
        color: #ffffff;
    }
    
    QLabel {
        color: #ffffff;
    }
    
    .info-label {
        color: #aaaaaa;
    }
    
    QStatusBar {
        background-color: #404040;
        border-top: 1px solid #555555;
        color: #aaaaaa;
    }
    
    QTabWidget::pane {
        border: 1px solid #555555;
        background-color: #3c3c3c;
    }
    
    QTabBar::tab {
        background-color: #404040;
        border: 1px solid #555555;
        color: #ffffff;
    }
    
    QTabBar::tab:selected {
        background-color: #3c3c3c;
        border-bottom-color: #3c3c3c;
    }
    """


def get_status_colors() -> Dict[str, str]:
    """Get status color mapping."""
    return {
        'success': '#28a745',
        'warning': '#fd7e14', 
        'error': '#dc3545',
        'info': '#17a2b8',
        'secondary': '#6c757d'
    }
