from PIL import Image
import io
from pathlib import Path
from app.logger import logger


class ImageCompressor:
    """图片压缩工具"""

    @staticmethod
    def compress_image(image_data: bytes, quality: int = 85, max_size: int = 1024 * 1024) -> bytes:
        """
        压缩图片
        quality: 图片质量 (1-100)
        max_size: 最大尺寸（字节）
        """
        try:
            img = Image.open(io.BytesIO(image_data))

            # 转换RGBA到RGB
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            # 压缩图片
            output = io.BytesIO()
            current_quality = quality

            while current_quality > 20:
                output.seek(0)
                output.truncate()
                img.save(output, format='JPEG', quality=current_quality, optimize=True)

                if output.tell() <= max_size or current_quality <= 20:
                    break

                current_quality -= 5

            compressed_data = output.getvalue()

            # 计算压缩率
            original_size = len(image_data)
            compressed_size = len(compressed_data)
            compression_ratio = (1 - compressed_size / original_size) * 100

            logger.info(
                f"Image compressed: {original_size} -> {compressed_size} bytes ({compression_ratio:.2f}% reduction)")

            return compressed_data

        except Exception as e:
            logger.error(f"Image compression failed: {e}")
            return image_data


image_compressor = ImageCompressor()