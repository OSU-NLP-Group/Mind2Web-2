"""Settings dialogs for keyword management and configuration."""

from __future__ import annotations
import logging
from typing import List, Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListView, QComboBox, QTextEdit, QTabWidget, QWidget, QFrame,
    QMessageBox, QSplitter, QGroupBox, QCheckBox
)

from ...models import KeywordDetector

logger = logging.getLogger(__name__)


class KeywordSettingsDialog(QDialog):
    """Dialog for managing keyword detection settings."""
    
    def __init__(self, keyword_detector: KeywordDetector, parent=None):
        super().__init__(parent)
        self.keyword_detector = keyword_detector
        
        # Models for keyword lists (mapped to two levels: definite/possible)
        self.high_priority_model = QStandardItemModel()    # definite
        self.medium_priority_model = QStandardItemModel()  # possible
        self.low_priority_model = QStandardItemModel()     # (unused; treated as possible)
        self.patterns_model = QStandardItemModel()
        
        self.setup_ui()
        self.setup_connections()
        self.load_current_settings()
        
        logger.debug("Keyword settings dialog initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        self.setWindowTitle("Keyword Detection Settings")
        self.setMinimumSize(700, 500)
        self.resize(800, 600)
        
        layout = QVBoxLayout(self)
        
        # Create tab widget
        tab_widget = QTabWidget()
        
        # Keywords tab
        keywords_tab = self.create_keywords_tab()
        tab_widget.addTab(keywords_tab, "Keywords")
        
        # Patterns tab
        patterns_tab = self.create_patterns_tab()
        tab_widget.addTab(patterns_tab, "Regex Patterns")
        
        # Settings tab
        settings_tab = self.create_settings_tab()
        tab_widget.addTab(settings_tab, "Settings")
        
        layout.addWidget(tab_widget)
        
        # Dialog buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        # Reset to defaults button
        self.reset_btn = QPushButton("Reset to Defaults")
        self.reset_btn.setToolTip("Reset all settings to default values")
        button_layout.addWidget(self.reset_btn)
        
        # Test button
        self.test_btn = QPushButton("Test Detection")
        self.test_btn.setToolTip("Test keyword detection with sample text")
        button_layout.addWidget(self.test_btn)
        
        # Cancel/OK buttons
        self.cancel_btn = QPushButton("Cancel")
        button_layout.addWidget(self.cancel_btn)
        
        self.ok_btn = QPushButton("OK")
        self.ok_btn.setDefault(True)
        button_layout.addWidget(self.ok_btn)
        
        layout.addLayout(button_layout)
    
    def create_keywords_tab(self) -> QWidget:
        """Create the keywords management tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions
        info_label = QLabel(
            "Manage keywords used for detecting problematic content. "
            "Higher priority keywords take precedence in detection results."
        )
        info_label.setWordWrap(True)
        info_label.setProperty("class", "info-label")
        layout.addWidget(info_label)
        
        # Create splitter for three priority levels
        splitter = QSplitter(Qt.Horizontal)
        
        # High priority keywords
        high_frame = self.create_keyword_priority_frame(
            "🔴 Definite", 
            self.high_priority_model,
            "Keywords that definitely indicate issues (blocks, verification, etc.)"
        )
        splitter.addWidget(high_frame)
        
        # Medium priority keywords
        medium_frame = self.create_keyword_priority_frame(
            "🟠 Possible",
            self.medium_priority_model,
            "Keywords that may indicate issues (captcha, connection failures, etc.)"
        )
        splitter.addWidget(medium_frame)
        
        # Low priority keywords
        low_frame = self.create_keyword_priority_frame(
            "(Optional) Other",
            self.low_priority_model,
            "Additional keywords treated as possible issues"
        )
        splitter.addWidget(low_frame)
        
        # Set equal sizes
        splitter.setSizes([250, 250, 250])
        layout.addWidget(splitter)
        
        return tab
    
    def create_keyword_priority_frame(self, title: str, model: QStandardItemModel, description: str) -> QFrame:
        """Create a frame for managing keywords of a specific priority."""
        frame = QGroupBox(title)
        layout = QVBoxLayout(frame)
        
        # Description
        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        desc_label.setProperty("class", "info-label")
        layout.addWidget(desc_label)
        
        # Keyword list
        list_view = QListView()
        list_view.setModel(model)
        list_view.setEditTriggers(QListView.DoubleClicked)
        layout.addWidget(list_view)
        
        # Add/remove controls
        controls_layout = QHBoxLayout()
        
        add_input = QLineEdit()
        add_input.setPlaceholderText("Enter new keyword...")
        controls_layout.addWidget(add_input)
        
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(lambda: self.add_keyword(model, add_input))
        controls_layout.addWidget(add_btn)
        
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda: self.remove_keyword(model, list_view))
        controls_layout.addWidget(remove_btn)
        
        layout.addLayout(controls_layout)
        
        # Store references for later use
        setattr(frame, 'list_view', list_view)
        setattr(frame, 'add_input', add_input)
        setattr(frame, 'model', model)
        
        return frame
    
    def create_patterns_tab(self) -> QWidget:
        """Create the regex patterns management tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Instructions
        info_label = QLabel(
            "Regular expression patterns for advanced content detection. "
            "These patterns are case-insensitive and support standard regex syntax."
        )
        info_label.setWordWrap(True)
        info_label.setProperty("class", "info-label")
        layout.addWidget(info_label)
        
        # Patterns list and editor
        splitter = QSplitter(Qt.Horizontal)
        
        # Left side: pattern list
        left_frame = QFrame()
        left_layout = QVBoxLayout(left_frame)
        
        left_layout.addWidget(QLabel("Current Patterns:"))
        
        self.patterns_list = QListView()
        self.patterns_list.setModel(self.patterns_model)
        left_layout.addWidget(self.patterns_list)
        
        # Pattern list controls
        pattern_controls_layout = QHBoxLayout()
        
        self.add_pattern_btn = QPushButton("Add New")
        pattern_controls_layout.addWidget(self.add_pattern_btn)
        
        self.edit_pattern_btn = QPushButton("Edit")
        self.edit_pattern_btn.setEnabled(False)
        pattern_controls_layout.addWidget(self.edit_pattern_btn)
        
        self.delete_pattern_btn = QPushButton("Delete")
        self.delete_pattern_btn.setEnabled(False)
        pattern_controls_layout.addWidget(self.delete_pattern_btn)
        
        left_layout.addLayout(pattern_controls_layout)
        splitter.addWidget(left_frame)
        
        # Right side: pattern editor
        right_frame = QFrame()
        right_layout = QVBoxLayout(right_frame)
        
        right_layout.addWidget(QLabel("Pattern Editor:"))
        
        # Pattern input
        pattern_input_layout = QHBoxLayout()
        pattern_input_layout.addWidget(QLabel("Pattern:"))
        
        self.pattern_input = QLineEdit()
        self.pattern_input.setPlaceholderText("Enter regex pattern...")
        pattern_input_layout.addWidget(self.pattern_input)
        
        right_layout.addLayout(pattern_input_layout)
        
        # Description input
        desc_input_layout = QHBoxLayout()
        desc_input_layout.addWidget(QLabel("Description:"))
        
        self.pattern_desc_input = QLineEdit()
        self.pattern_desc_input.setPlaceholderText("Enter pattern description...")
        desc_input_layout.addWidget(self.pattern_desc_input)
        
        right_layout.addLayout(desc_input_layout)
        
        # Test area
        right_layout.addWidget(QLabel("Test Pattern:"))
        
        self.pattern_test_input = QTextEdit()
        self.pattern_test_input.setMaximumHeight(100)
        self.pattern_test_input.setPlaceholderText("Enter test text here...")
        right_layout.addWidget(self.pattern_test_input)
        
        # Test button
        test_pattern_layout = QHBoxLayout()
        test_pattern_layout.addStretch()
        
        self.test_pattern_btn = QPushButton("Test Pattern")
        test_pattern_layout.addWidget(self.test_pattern_btn)
        
        right_layout.addLayout(test_pattern_layout)
        
        # Test results
        self.pattern_test_results = QLabel("Test results will appear here")
        self.pattern_test_results.setProperty("class", "info-label")
        self.pattern_test_results.setWordWrap(True)
        right_layout.addWidget(self.pattern_test_results)
        
        right_layout.addStretch()
        splitter.addWidget(right_frame)
        
        splitter.setSizes([350, 400])
        layout.addWidget(splitter)
        
        return tab
    
    def create_settings_tab(self) -> QWidget:
        """Create the general settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # General settings
        settings_group = QGroupBox("General Settings")
        settings_layout = QVBoxLayout(settings_group)
        
        # Case sensitivity (not implemented in current detector, but placeholder)
        self.case_sensitive_cb = QCheckBox("Case sensitive matching")
        self.case_sensitive_cb.setToolTip("Enable case sensitive keyword matching")
        settings_layout.addWidget(self.case_sensitive_cb)
        
        # Auto-save settings
        self.auto_save_cb = QCheckBox("Auto-save settings")
        self.auto_save_cb.setToolTip("Automatically save settings when changed")
        self.auto_save_cb.setChecked(True)
        settings_layout.addWidget(self.auto_save_cb)
        
        layout.addWidget(settings_group)
        
        # Statistics
        stats_group = QGroupBox("Current Statistics")
        stats_layout = QVBoxLayout(stats_group)
        
        self.stats_label = QLabel("Loading statistics...")
        self.stats_label.setProperty("class", "info-label")
        stats_layout.addWidget(self.stats_label)
        
        layout.addWidget(stats_group)
        
        layout.addStretch()
        
        return tab
    
    def setup_connections(self):
        """Setup signal connections."""
        # Dialog buttons
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        self.reset_btn.clicked.connect(self.reset_to_defaults)
        self.test_btn.clicked.connect(self.show_test_dialog)
        
        # Patterns tab
        self.add_pattern_btn.clicked.connect(self.add_new_pattern)
        self.edit_pattern_btn.clicked.connect(self.edit_selected_pattern)
        self.delete_pattern_btn.clicked.connect(self.delete_selected_pattern)
        self.test_pattern_btn.clicked.connect(self.test_current_pattern)
        
        # Pattern list selection
        self.patterns_list.selectionModel().currentChanged.connect(self.on_pattern_selection_changed)
    
    def load_current_settings(self):
        """Load current settings from keyword detector."""
        # Load keywords by priority
        keywords = self.keyword_detector.get_all_keywords()
        # High priority (definite)
        self.high_priority_model.clear()
        for keyword in keywords.get('definite', []):
            item = QStandardItem(keyword)
            self.high_priority_model.appendRow(item)
        
        # Possible  
        self.medium_priority_model.clear()
        for keyword in keywords.get('possible', []):
            item = QStandardItem(keyword)
            self.medium_priority_model.appendRow(item)
        
        # Other (kept empty by default)
        self.low_priority_model.clear()
        
        # Load patterns
        self.patterns_model.clear()
        patterns = self.keyword_detector.get_all_patterns()
        for pattern_tuple in patterns:
            # pattern_tuple may be (pattern, desc, level)
            if len(pattern_tuple) == 3:
                pattern, description, level = pattern_tuple
            else:
                pattern, description = pattern_tuple
            item = QStandardItem(f"{description} ({pattern})")
            item.setData((pattern, description), Qt.UserRole)
            self.patterns_model.appendRow(item)
        
        # Update statistics
        self.update_statistics()
    
    def update_statistics(self):
        """Update statistics display."""
        keywords = self.keyword_detector.get_all_keywords()
        patterns = self.keyword_detector.get_all_patterns()
        
        total_keywords = len(keywords['high']) + len(keywords['medium']) + len(keywords['low'])
        
        stats_text = f"""
        Total Keywords: {total_keywords}
        • High Priority: {len(keywords['high'])}
        • Medium Priority: {len(keywords['medium'])}
        • Low Priority: {len(keywords['low'])}
        
        Regex Patterns: {len(patterns)}
        """
        
        self.stats_label.setText(stats_text.strip())
    
    def add_keyword(self, model: QStandardItemModel, input_field: QLineEdit):
        """Add keyword to the specified model."""
        keyword = input_field.text().strip()
        if not keyword:
            return
        
        # Check if keyword already exists in any model
        if self.keyword_exists_in_any_model(keyword):
            QMessageBox.warning(self, "Duplicate Keyword", f"Keyword '{keyword}' already exists.")
            return
        
        # Add to model
        item = QStandardItem(keyword)
        model.appendRow(item)
        
        # Clear input
        input_field.clear()
        
        logger.debug(f"Added keyword: {keyword}")
    
    def remove_keyword(self, model: QStandardItemModel, list_view: QListView):
        """Remove selected keyword from model."""
        selection = list_view.selectionModel().currentIndex()
        if not selection.isValid():
            return
        
        keyword = model.data(selection, Qt.DisplayRole)
        model.removeRow(selection.row())
        
        logger.debug(f"Removed keyword: {keyword}")
    
    def keyword_exists_in_any_model(self, keyword: str) -> bool:
        """Check if keyword exists in any priority model."""
        keyword_lower = keyword.lower()
        
        for model in [self.high_priority_model, self.medium_priority_model, self.low_priority_model]:
            for row in range(model.rowCount()):
                existing = model.item(row).text().lower()
                if existing == keyword_lower:
                    return True
        
        return False
    
    def add_new_pattern(self):
        """Add new regex pattern."""
        self.pattern_input.clear()
        self.pattern_desc_input.clear()
        self.pattern_test_input.clear()
        self.pattern_test_results.setText("Enter pattern and test it before adding")
        self.pattern_input.setFocus()
    
    def edit_selected_pattern(self):
        """Edit the selected pattern."""
        selection = self.patterns_list.selectionModel().currentIndex()
        if not selection.isValid():
            return
        
        pattern_data = self.patterns_model.data(selection, Qt.UserRole)
        if pattern_data:
            pattern, description = pattern_data
            self.pattern_input.setText(pattern)
            self.pattern_desc_input.setText(description)
    
    def delete_selected_pattern(self):
        """Delete the selected pattern."""
        selection = self.patterns_list.selectionModel().currentIndex()
        if not selection.isValid():
            return
        
        pattern_display = self.patterns_model.data(selection, Qt.DisplayRole)
        reply = QMessageBox.question(
            self,
            "Delete Pattern",
            f"Are you sure you want to delete this pattern?\n\n{pattern_display}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.patterns_model.removeRow(selection.row())
    
    def test_current_pattern(self):
        """Test the current pattern against test text."""
        pattern = self.pattern_input.text().strip()
        test_text = self.pattern_test_input.toPlainText().strip()
        
        if not pattern:
            self.pattern_test_results.setText("Please enter a pattern to test")
            return
        
        if not test_text:
            self.pattern_test_results.setText("Please enter test text")
            return
        
        try:
            import re
            matches = re.finditer(pattern, test_text, re.IGNORECASE | re.MULTILINE)
            match_list = list(matches)
            
            if match_list:
                result_text = f"✅ Pattern matched {len(match_list)} time(s):\\n\\n"
                for i, match in enumerate(match_list[:5]):  # Show first 5 matches
                    result_text += f"{i+1}. \"{match.group()}\" at position {match.start()}-{match.end()}\\n"
                
                if len(match_list) > 5:
                    result_text += f"... and {len(match_list) - 5} more matches"
            else:
                result_text = "❌ Pattern did not match the test text"
            
            self.pattern_test_results.setText(result_text)
            
        except re.error as e:
            self.pattern_test_results.setText(f"❌ Invalid regex pattern: {str(e)}")
    
    def on_pattern_selection_changed(self, current, previous):
        """Handle pattern selection change."""
        has_selection = current.isValid()
        self.edit_pattern_btn.setEnabled(has_selection)
        self.delete_pattern_btn.setEnabled(has_selection)
    
    def reset_to_defaults(self):
        """Reset all settings to default values."""
        reply = QMessageBox.question(
            self,
            "Reset to Defaults",
            "Are you sure you want to reset all keyword detection settings to default values?\\n\\n"
            "This will remove all custom keywords and patterns.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # Reset keyword detector to defaults
            self.keyword_detector.definite_keywords.clear()
            self.keyword_detector.possible_keywords.clear()
            self.keyword_detector.patterns.clear()
            
            self.keyword_detector._load_default_rules()
            
            # Reload UI
            self.load_current_settings()
            
            QMessageBox.information(self, "Reset Complete", "Settings have been reset to defaults.")
    
    def show_test_dialog(self):
        """Show keyword detection test dialog."""
        # TODO: Implement test dialog
        QMessageBox.information(self, "Test Detection", "Test dialog will be implemented.")
    
    def accept(self):
        """Apply changes and close dialog."""
        try:
            # Apply keyword changes
            self.apply_keyword_changes()
            
            # Apply pattern changes
            self.apply_pattern_changes()
            
            # Save settings if auto-save is enabled
            if self.auto_save_cb.isChecked():
                self.keyword_detector.save_config()
            
            super().accept()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to apply settings: {str(e)}")
    
    def apply_keyword_changes(self):
        """Apply keyword changes to detector."""
        # Clear existing keywords
        self.keyword_detector.definite_keywords.clear()
        self.keyword_detector.possible_keywords.clear()
        
        # Add keywords from models: high -> definite; medium/low -> possible
        for row in range(self.high_priority_model.rowCount()):
            keyword = self.high_priority_model.item(row).text()
            self.keyword_detector.definite_keywords.add(keyword.lower())
        
        for row in range(self.medium_priority_model.rowCount()):
            keyword = self.medium_priority_model.item(row).text()
            self.keyword_detector.possible_keywords.add(keyword.lower())
        
        for row in range(self.low_priority_model.rowCount()):
            keyword = self.low_priority_model.item(row).text()
            self.keyword_detector.possible_keywords.add(keyword.lower())
    
    def apply_pattern_changes(self):
        """Apply pattern changes to detector."""
        # Get current patterns from input fields and add/update if valid
        current_pattern = self.pattern_input.text().strip()
        current_desc = self.pattern_desc_input.text().strip()
        
        if current_pattern and current_desc:
            # Add or update current pattern
            self.keyword_detector.add_pattern(current_pattern, current_desc)
        
        # Note: For full pattern management, we'd need to track which patterns
        # were deleted from the list and remove them accordingly
