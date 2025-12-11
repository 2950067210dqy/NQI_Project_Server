from loguru import logger
import sys
from pathlib import Path
from app.config_ini import config


def setup_logger():
    # 创建日志目录
    config.log_dir.mkdir(parents=True, exist_ok=True)

    # 移除默认处理器
    logger.remove()

    # 添加控制台输出
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=config.log_level
    )

    # 添加文件输出
    logger.add(
        config.log_dir / "server_{time:YYYY-MM-DD}.log",
        rotation=config.config.get('logging', 'log_rotation'),
        retention=config.config.get('logging', 'log_retention'),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level=config.log_level,
        encoding='utf-8'
    )

    return logger


logger = setup_logger()