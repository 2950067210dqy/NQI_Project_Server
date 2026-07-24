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
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
            "pid={process.id} | thread={thread.name}:{thread.id} | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level=config.log_level,
        backtrace=True,
        diagnose=False,
        enqueue=True,
        catch=True,
    )

    # 添加文件输出
    logger.add(
        config.log_dir / "server_{time:YYYY-MM-DD}.log",
        rotation=config.config.get('logging', 'log_rotation', fallback='10 MB'),
        retention=config.config.get('logging', 'log_retention', fallback='30 days'),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "pid={process.id} | thread={thread.name}:{thread.id} | "
            "{name}:{function}:{line} - {message}"
        ),
        level=config.log_level,
        encoding='utf-8',
        backtrace=True,
        diagnose=False,
        enqueue=True,
        catch=True,
    )

    return logger


logger = setup_logger()