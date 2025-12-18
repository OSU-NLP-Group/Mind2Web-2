"""Image processing utilities for screenshots and image handling."""

from __future__ import annotations
import io
from typing import Tuple, Optional
import logging

from PIL import Image, ImageOps
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtCore import QSize

logger = logging.getLogger(__name__)

# Disable PIL image size warnings for large images
Image.MAX_IMAGE_PIXELS = None


class ImageProcessor:
    """Utilities for image processing and conversion."""
    
    @staticmethod
    def resize_image_to_size(image_bytes: bytes, target_width: int, target_height: int) -> bytes:
        """Resize image to specific dimensions.
        
        Args:
            image_bytes: Original image data
            target_width: Target width in pixels
            target_height: Target height in pixels
            
        Returns:
            Resized image data as bytes
        """
        try:
            # Open image
            image = Image.open(io.BytesIO(image_bytes))
            
            # Resize to exact dimensions
            image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
            
            # Save to bytes
            output = io.BytesIO()
            image.save(output, format='PNG', optimize=True)
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Failed to resize image to {target_width}x{target_height}: {e}")
            return image_bytes
    
    @staticmethod
    def resize_for_display(image_bytes: bytes, max_width: int = 800, 
                          max_height: int = 600) -> bytes:
        """Resize image for display while maintaining aspect ratio.
        
        Args:
            image_bytes: Original image data
            max_width: Maximum display width
            max_height: Maximum display height
            
        Returns:
            Resized image data as bytes
        """
        try:
            # Open image
            image = Image.open(io.BytesIO(image_bytes))
            
            # Calculate resize ratio
            width_ratio = max_width / image.width
            height_ratio = max_height / image.height
            scale_ratio = min(width_ratio, height_ratio, 1.0)  # Don't upscale
            
            # Resize if needed
            if scale_ratio < 1.0:
                new_width = int(image.width * scale_ratio)
                new_height = int(image.height * scale_ratio)
                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # Save to bytes
            output = io.BytesIO()
            image.save(output, format='PNG', optimize=True)
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Failed to resize image: {e}")
            return image_bytes
    
    @staticmethod
    def crop_image(image_bytes: bytes, max_height: int = 6000) -> bytes:
        """Crop image height to prevent excessive memory usage.
        
        Args:
            image_bytes: Original image data  
            max_height: Maximum height in pixels
            
        Returns:
            Cropped image data as bytes
        """
        try:
            image = Image.open(io.BytesIO(image_bytes))
            
            # Crop if too tall
            if image.height > max_height:
                image = image.crop((0, 0, image.width, max_height))
                
                # Save to bytes
                output = io.BytesIO()
                image.save(output, format='PNG', optimize=True)
                return output.getvalue()
            
            return image_bytes
            
        except Exception as e:
            logger.error(f"Failed to crop image: {e}")
            return image_bytes
    
    @staticmethod
    def optimize_screenshot(image_bytes: bytes) -> bytes:
        """Optimize screenshot for storage and display.
        
        Args:
            image_bytes: Original screenshot data
            
        Returns:
            Optimized image data as bytes
        """
        try:
            image = Image.open(io.BytesIO(image_bytes))
            
            # Crop excessive height
            if image.height > 6000:
                image = image.crop((0, 0, image.width, 6000))
            
            # Convert to RGB if needed (for better compression)
            if image.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', image.size, (255, 255, 255))
                if image.mode == 'RGBA':
                    background.paste(image, mask=image.split()[-1])
                else:
                    background.paste(image, mask=image.split()[-1])
                image = background
            elif image.mode != 'RGB':
                image = image.convert('RGB')
            
            # Save with optimization
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=85, optimize=True)
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Failed to optimize screenshot: {e}")
            return image_bytes
    
    @staticmethod
    def pil_to_qpixmap(pil_image: Image.Image) -> QPixmap:
        """Convert PIL image to QPixmap.
        
        Args:
            pil_image: PIL Image object
            
        Returns:
            QPixmap for display in Qt widgets
        """
        try:
            # Convert to RGBA if not already
            if pil_image.mode != 'RGBA':
                pil_image = pil_image.convert('RGBA')
            
            # Create QImage
            width, height = pil_image.size
            image_data = pil_image.tobytes('raw', 'RGBA')
            q_image = QImage(image_data, width, height, QImage.Format_RGBA8888)
            
            # Convert to QPixmap
            return QPixmap.fromImage(q_image)
            
        except Exception as e:
            logger.error(f"Failed to convert PIL to QPixmap: {e}")
            return QPixmap()
    
    @staticmethod
    def bytes_to_qpixmap(image_bytes: bytes) -> QPixmap:
        """Convert image bytes to QPixmap.
        
        Args:
            image_bytes: Image data as bytes
            
        Returns:
            QPixmap for display in Qt widgets
        """
        try:
            pil_image = Image.open(io.BytesIO(image_bytes))
            return ImageProcessor.pil_to_qpixmap(pil_image)
        except Exception as e:
            logger.error(f"Failed to convert bytes to QPixmap: {e}")
            return QPixmap()
    
    @staticmethod
    def get_image_info(image_bytes: bytes) -> Tuple[int, int, str, int]:
        """Get image information.
        
        Args:
            image_bytes: Image data as bytes
            
        Returns:
            Tuple of (width, height, format, file_size)
        """
        try:
            image = Image.open(io.BytesIO(image_bytes))
            return (
                image.width,
                image.height, 
                image.format or 'Unknown',
                len(image_bytes)
            )
        except Exception as e:
            logger.error(f"Failed to get image info: {e}")
            return (0, 0, 'Unknown', len(image_bytes))
    
    @staticmethod
    def create_thumbnail(image_bytes: bytes, size: Tuple[int, int] = (200, 150)) -> bytes:
        """Create thumbnail from image.
        
        Args:
            image_bytes: Original image data
            size: Thumbnail size as (width, height)
            
        Returns:
            Thumbnail image data as bytes
        """
        try:
            image = Image.open(io.BytesIO(image_bytes))
            
            # Create thumbnail
            image.thumbnail(size, Image.Resampling.LANCZOS)
            
            # Save to bytes
            output = io.BytesIO()
            image.save(output, format='PNG', optimize=True)
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Failed to create thumbnail: {e}")
            return image_bytes
    
    @staticmethod
    def is_valid_image(image_bytes: bytes) -> bool:
        """Check if bytes represent a valid image.
        
        Args:
            image_bytes: Image data to validate
            
        Returns:
            True if valid image, False otherwise
        """
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.verify()  # Verify image integrity
            return True
        except Exception:
            return False