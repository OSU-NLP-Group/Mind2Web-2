"""MHTML file upload and processing dialog."""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QFileDialog, QMessageBox, QFrame, QProgressBar
)

from ...utils import MHTMLParser, extract_text_and_image

logger = logging.getLogger(__name__)


class MHTMLUploadDialog(QDialog):
    """Dialog for uploading and processing MHTML files."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Results
        self.result_url: Optional[str] = None
        self.result_text: Optional[str] = None
        self.result_image: Optional[bytes] = None
        
        self.setup_ui()
        self.setup_connections()
        
        logger.debug("MHTML upload dialog initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        self.setWindowTitle("Upload MHTML File")
        self.setMinimumSize(600, 500)
        self.resize(700, 600)
        
        layout = QVBoxLayout(self)
        
        # Instructions
        info_label = QLabel(
            "Upload an MHTML file to extract URL, text content, and screenshot. "
            "MHTML files are commonly saved from browsers when saving complete web pages."
        )
        info_label.setWordWrap(True)
        info_label.setProperty("class", "info-label")
        layout.addWidget(info_label)
        
        # File selection section
        file_frame = QFrame()
        file_frame.setProperty("class", "panel-frame")
        file_layout = QVBoxLayout(file_frame)
        
        file_layout.addWidget(QLabel("Select MHTML File:"))
        
        file_select_layout = QHBoxLayout()
        
        self.file_path_input = QLineEdit()
        self.file_path_input.setPlaceholderText("No file selected...")
        self.file_path_input.setReadOnly(True)
        file_select_layout.addWidget(self.file_path_input)
        
        self.browse_btn = QPushButton("Browse...")
        file_select_layout.addWidget(self.browse_btn)
        
        file_layout.addLayout(file_select_layout)
        
        # Process button
        process_layout = QHBoxLayout()
        process_layout.addStretch()
        
        self.process_btn = QPushButton("📄 Process MHTML")
        self.process_btn.setEnabled(False)
        process_layout.addWidget(self.process_btn)
        
        file_layout.addLayout(process_layout)
        
        layout.addWidget(file_frame)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # Status
        self.status_label = QLabel("Select an MHTML file to begin processing")
        self.status_label.setProperty("class", "info-label")
        layout.addWidget(self.status_label)
        
        # Results section
        results_frame = QFrame()
        results_frame.setProperty("class", "panel-frame")
        results_layout = QVBoxLayout(results_frame)
        
        results_layout.addWidget(QLabel("Processing Results:"))
        
        # URL result
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("Extracted URL:"))
        
        self.url_result = QLineEdit()
        self.url_result.setReadOnly(True)
        self.url_result.setPlaceholderText("URL will appear here after processing...")
        url_layout.addWidget(self.url_result)
        
        results_layout.addLayout(url_layout)
        
        # Text content preview
        results_layout.addWidget(QLabel("Text Content Preview:"))
        
        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)
        self.text_preview.setMaximumHeight(200)
        self.text_preview.setPlaceholderText("Extracted text will appear here...")
        results_layout.addWidget(self.text_preview)
        
        # Image info
        self.image_info = QLabel("No image extracted")
        self.image_info.setProperty("class", "info-label")
        results_layout.addWidget(self.image_info)
        
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
        self.browse_btn.clicked.connect(self.browse_file)
        self.process_btn.clicked.connect(self.process_mhtml)
        self.cancel_btn.clicked.connect(self.reject)
        self.use_btn.clicked.connect(self.accept)
        
        self.file_path_input.textChanged.connect(self._on_file_path_changed)
    
    def browse_file(self):
        """Browse for MHTML file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select MHTML File",
            str(Path.home()),
            "MHTML Files (*.mhtml *.mht);;All Files (*)"
        )
        
        if file_path:
            self.file_path_input.setText(file_path)
    
    def _on_file_path_changed(self, path: str):
        """Handle file path change."""
        self.process_btn.setEnabled(bool(path.strip()))
        
        # Clear previous results
        self.clear_results()
    
    def process_mhtml(self):
        """Process the selected MHTML file."""
        file_path = self.file_path_input.text().strip()
        
        if not file_path or not Path(file_path).exists():
            QMessageBox.warning(self, "Invalid File", "Please select a valid MHTML file.")
            return
        
        # Show progress
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("Processing MHTML file...")
        self.process_btn.setEnabled(False)
        
        try:
            # Process MHTML file
            url, text, image = extract_text_and_image(file_path)
            
            if not url and not text and not image:
                raise ValueError("No valid content found in MHTML file")
            
            # Store results
            self.result_url = url
            self.result_text = text
            self.result_image = image
            
            # Update UI
            self.url_result.setText(url or "No URL found")
            
            if text:
                preview_text = text[:2000] + ("..." if len(text) > 2000 else "")
                self.text_preview.setPlainText(preview_text)
            else:
                self.text_preview.setPlainText("No text content extracted")
            
            if image:
                image_size = len(image) // 1024
                self.image_info.setText(f"Image extracted: {image_size}KB")
            else:
                self.image_info.setText("No image found in MHTML")
            
            # Update status
            self.status_label.setText("✅ MHTML processed successfully!")
            self.status_label.setProperty("class", "success-label")
            self.use_btn.setEnabled(True)
            
            logger.info(f"Successfully processed MHTML: {file_path}")
            
        except Exception as e:
            logger.error(f"Failed to process MHTML file {file_path}: {e}")
            
            self.status_label.setText(f"❌ Failed to process MHTML: {str(e)}")
            self.status_label.setProperty("class", "error-label")
            
            QMessageBox.critical(
                self,
                "Processing Failed",
                f"Failed to process MHTML file:\\n\\n{str(e)}\\n\\n"
                "Please ensure the file is a valid MHTML file saved from a web browser."
            )
        
        finally:
            # Hide progress
            self.progress_bar.setVisible(False)
            self.process_btn.setEnabled(True)
            
            # Refresh style
            self.status_label.setStyle(self.status_label.style())
    
    def clear_results(self):
        """Clear all results."""
        self.result_url = None
        self.result_text = None
        self.result_image = None
        
        self.url_result.clear()
        self.text_preview.clear()
        self.image_info.setText("No image extracted")
        self.status_label.setText("Results cleared")
        self.status_label.setProperty("class", "info-label")
        self.status_label.setStyle(self.status_label.style())
        
        self.use_btn.setEnabled(False)
    
    def get_results(self) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
        """Get the extracted results.
        
        Returns:
            Tuple of (url, text, image_bytes)
        """
        return self.result_url, self.result_text, self.result_image


# Convenience function
def upload_mhtml_dialog(parent=None) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
    """Show MHTML upload dialog and return results.
    
    Returns:
        Tuple of (url, text, image) or (None, None, None) if cancelled
    """
    dialog = MHTMLUploadDialog(parent)
    
    if dialog.exec() == QDialog.Accepted:
        return dialog.get_results()
    
    return None, None, None