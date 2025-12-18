"""Main entry point for the Cache Manager application."""

import sys
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPalette, QColor

from .widgets import CacheManagerMainWindow
from .resources import get_app_stylesheet


def setup_logging(log_level: str = "INFO") -> None:
    """Setup application logging."""
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # Convert string level to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    logging.basicConfig(
        level=numeric_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            # Optionally add file handler
            # logging.FileHandler("cache_manager.log")
        ]
    )
    
    # Reduce Qt logging noise
    logging.getLogger("PySide6").setLevel(logging.WARNING)
    logging.getLogger("qt").setLevel(logging.WARNING)


def check_dependencies() -> bool:
    """Check if all required dependencies are available."""
    try:
        import PySide6
        from PySide6.QtWebEngineWidgets import QWebEngineView
        return True
    except ImportError as e:
        QMessageBox.critical(
            None,
            "Missing Dependencies",
            f"Required dependency missing: {e}\n\n"
            "Please install PySide6 with WebEngine support:\n"
            "pip install PySide6 PySide6-WebEngine"
        )
        return False


def _apply_force_light_palette(app: QApplication) -> None:
    """Force a light palette regardless of system dark mode (macOS safe)."""
    try:
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#ffffff"))
        palette.setColor(QPalette.WindowText, QColor("#111111"))
        palette.setColor(QPalette.Base, QColor("#ffffff"))
        palette.setColor(QPalette.AlternateBase, QColor("#f6f6f6"))
        palette.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
        palette.setColor(QPalette.ToolTipText, QColor("#111111"))
        palette.setColor(QPalette.Text, QColor("#111111"))
        palette.setColor(QPalette.Button, QColor("#ffffff"))
        palette.setColor(QPalette.ButtonText, QColor("#111111"))
        palette.setColor(QPalette.BrightText, QColor("#ff0000"))
        palette.setColor(QPalette.Link, QColor("#2563eb"))
        palette.setColor(QPalette.Highlight, QColor("#007acc"))
        palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        app.setPalette(palette)
    except Exception:
        pass


def create_application() -> QApplication:
    """Create and configure QApplication."""
    # Enable high DPI scaling
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    app = QApplication(sys.argv)
    # Use Fusion style and a fixed light palette to avoid macOS auto dark mode
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    _apply_force_light_palette(app)
    
    # Set application metadata
    app.setApplicationName("Cache Manager")
    app.setApplicationVersion("2.0.0")
    app.setOrganizationName("Mind2Web2")
    app.setOrganizationDomain("mind2web2.ai")
    
    # Set application icon (if available)
    # app.setWindowIcon(QIcon(":/icons/cache_manager.png"))
    
    return app


def main(cache_folder: Optional[str] = None, log_level: str = "INFO") -> int:
    """Main application entry point.
    
    Args:
        cache_folder: Optional path to cache folder to load on startup
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        
    Returns:
        Application exit code
    """
    # Setup logging
    setup_logging(log_level)
    logger = logging.getLogger(__name__)
    
    logger.info("Starting Cache Manager v2.0")
    
    # Check dependencies
    if not check_dependencies():
        return 1
    
    try:
        # Create application
        app = create_application()
        
        # Apply global stylesheet
        app.setStyleSheet(get_app_stylesheet())
        
        # Create main window
        main_window = CacheManagerMainWindow()
        
        # Load cache folder if provided
        if cache_folder:
            cache_path = Path(cache_folder)
            if cache_path.exists() and cache_path.is_dir():
                logger.info(f"Loading cache folder: {cache_folder}")
                # Use QTimer to load after UI is fully initialized
                QTimer.singleShot(100, lambda: main_window.load_cache_folder(cache_folder))
            else:
                logger.warning(f"Invalid cache folder: {cache_folder}")
                QMessageBox.warning(
                    main_window,
                    "Invalid Cache Folder",
                    f"The specified cache folder does not exist or is not a directory:\n{cache_folder}"
                )
        
        # Show main window
        main_window.show()
        
        # Center window on screen
        screen = app.primaryScreen().availableGeometry()
        window_geometry = main_window.frameGeometry()
        window_geometry.moveCenter(screen.center())
        main_window.move(window_geometry.topLeft())
        
        logger.info("Cache Manager started successfully")
        
        # Start event loop
        return app.exec()
        
    except Exception as e:
        logger.error(f"Failed to start application: {e}", exc_info=True)
        
        # Show error dialog if possible
        try:
            QMessageBox.critical(
                None,
                "Application Error",
                f"Failed to start Cache Manager:\n\n{str(e)}\n\n"
                "Please check the logs for more details."
            )
        except Exception:
            pass  # QApplication might not be available
        
        return 1


def cli_main() -> int:
    """Command line interface entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Modern Cache Manager for Mind2Web2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cache_manager                           # Start with empty workspace
  cache_manager /path/to/cache/agent      # Load specific agent cache
  cache_manager --log-level DEBUG         # Enable debug logging
        """
    )
    
    parser.add_argument(
        "cache_folder",
        nargs="?",
        help="Path to cache agent folder to load on startup"
    )
    
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging level (default: INFO)"
    )
    
    parser.add_argument(
        "--version",
        action="version",
        version="Cache Manager v2.0.0"
    )
    
    args = parser.parse_args()
    
    return main(args.cache_folder, args.log_level)


if __name__ == "__main__":
    sys.exit(cli_main())
