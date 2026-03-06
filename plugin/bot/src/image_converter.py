"""
Image Converter Utility.

Handles conversion of various image formats (HEIC, etc.) to web-friendly formats (JPG/PNG).
Uses pillow-heif for HEIC support.
"""
import os
import logging
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Try to import pillow-heif for HEIC support
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORTED = True
    logger.info("HEIC support enabled via pillow-heif")
except ImportError:
    HEIC_SUPPORTED = False
    logger.warning("pillow-heif not installed - HEIC files won't be supported")

from PIL import Image


SUPPORTED_INPUT_FORMATS = {'.heic', '.heif', '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
OUTPUT_FORMAT = 'JPEG'
OUTPUT_EXTENSION = '.jpg'
DEFAULT_QUALITY = 95


def is_heic_file(path: str) -> bool:
    """Check if a file is HEIC format."""
    return Path(path).suffix.lower() in {'.heic', '.heif'}


def is_pdf_file(path: str) -> bool:
    """Check if a file is PDF format."""
    return Path(path).suffix.lower() == '.pdf'


def is_supported_image(path: str) -> bool:
    """Check if a file is a supported image format."""
    return Path(path).suffix.lower() in SUPPORTED_INPUT_FORMATS


def convert_image(
    input_path: str,
    output_dir: Optional[str] = None,
    quality: int = DEFAULT_QUALITY,
    force: bool = False
) -> str:
    """
    Convert an image to JPG format.
    
    Args:
        input_path: Path to input image
        output_dir: Output directory (default: same as input with 'converted' subfolder)
        quality: JPEG quality (1-100)
        force: Force conversion even if output exists
        
    Returns:
        Path to converted image
        
    Raises:
        ValueError: If input format not supported
        FileNotFoundError: If input file doesn't exist
    """
    input_path = Path(input_path)
    
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    if not is_supported_image(str(input_path)):
        raise ValueError(f"Unsupported image format: {input_path.suffix}")
    
    if is_heic_file(str(input_path)) and not HEIC_SUPPORTED:
        raise ValueError("HEIC support not available - install pillow-heif")
    
    # Determine output path
    if output_dir:
        output_dir = Path(output_dir)
    else:
        output_dir = input_path.parent / "converted"
    
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (input_path.stem + OUTPUT_EXTENSION)
    
    # Skip if already converted (unless force)
    if output_path.exists() and not force:
        logger.info(f"Already converted: {output_path}")
        return str(output_path)
    
    # Convert
    logger.info(f"Converting: {input_path} -> {output_path}")
    
    try:
        with Image.open(input_path) as img:
            # Convert to RGB if necessary (for formats like PNG with transparency)
            if img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save as JPEG
            img.save(output_path, OUTPUT_FORMAT, quality=quality)
            
        logger.info(f"Converted successfully: {output_path}")
        return str(output_path)
        
    except Exception as e:
        logger.error(f"Failed to convert {input_path}: {e}")
        raise


def convert_directory(
    input_dir: str,
    output_dir: Optional[str] = None,
    quality: int = DEFAULT_QUALITY,
    force: bool = False
) -> List[Tuple[str, str]]:
    """
    Convert all supported images in a directory.
    
    Args:
        input_dir: Directory containing images
        output_dir: Output directory (default: 'converted' subfolder)
        quality: JPEG quality (1-100)
        force: Force conversion even if outputs exist
        
    Returns:
        List of (input_path, output_path) tuples
    """
    input_dir = Path(input_dir)
    
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    
    # Find all supported images
    images = [
        f for f in input_dir.iterdir()
        if f.is_file() and is_supported_image(str(f))
    ]
    
    if not images:
        logger.warning(f"No supported images found in {input_dir}")
        return []
    
    logger.info(f"Found {len(images)} images to convert")
    
    # Convert each
    results = []
    for img_path in sorted(images):
        try:
            output_path = convert_image(str(img_path), output_dir, quality, force)
            results.append((str(img_path), output_path))
        except Exception as e:
            logger.error(f"Failed to convert {img_path}: {e}")
    
    logger.info(f"Converted {len(results)}/{len(images)} images")
    return results


def ensure_jpg(path: str, output_dir: Optional[str] = None) -> str:
    """
    Ensure an image is in JPG format, converting if necessary.
    
    If already JPG/JPEG, returns the original path.
    Otherwise, converts and returns the new path.
    
    Args:
        path: Path to image
        output_dir: Output directory for conversions
        
    Returns:
        Path to JPG image (original or converted)
    """
    path = Path(path)
    
    # Already JPG - return as is
    if path.suffix.lower() in {'.jpg', '.jpeg'}:
        return str(path)
    
    # Need conversion
    return convert_image(str(path), output_dir)


# CLI interface
if __name__ == "__main__":
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) < 2:
        print("Usage: python image_converter.py <input_path_or_dir> [output_dir]")
        sys.exit(1)
    
    input_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None
    
    if os.path.isdir(input_path):
        results = convert_directory(input_path, output_dir)
        print(f"\nConverted {len(results)} images:")
        for inp, out in results:
            print(f"  {inp} -> {out}")
    else:
        output = convert_image(input_path, output_dir)
        print(f"Converted: {output}")
