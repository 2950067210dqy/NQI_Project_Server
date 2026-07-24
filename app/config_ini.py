import configparser
import os
from pathlib import Path
from typing import List


class Config:
    def __init__(self, config_file: str = "config.ini"):
        self.config = configparser.ConfigParser()
        self.base_dir = Path(__file__).resolve().parent.parent
        self.config_file = self._resolve_config_file(config_file)
        # 先放入兜底配置，避免线上工作目录不一致或缺少非关键配置段时启动即崩溃。
        self.config.read_dict(self._default_config())
        self.loaded_files = self.config.read(self.config_file, encoding='utf-8')

    def _resolve_config_file(self, config_file: str) -> Path:
        """解析配置文件路径，优先使用环境变量，其次使用项目根目录下的 config.ini。"""
        env_config = os.getenv("NQI_SERVER_CONFIG")
        if env_config:
            return Path(env_config).expanduser().resolve()
        candidate = Path(config_file)
        if candidate.is_absolute():
            return candidate
        project_candidate = self.base_dir / candidate
        if project_candidate.exists():
            return project_candidate
        return Path.cwd() / candidate

    @staticmethod
    def _default_config() -> dict:
        """服务端默认配置，用于缺配置段时保持服务可启动。"""
        return {
            'database': {
                'host': 'localhost',
                'port': '3306',
                'user': 'NQI_Server',
                'password': 'nqiserver',
                'database': 'nqi_system',
                'pool_size': '20',
                'max_overflow': '40',
                'pool_timeout': '10',
                'pool_recycle': '1800',
            },
            'server': {
                'host': '0.0.0.0',
                'port': '8000',
                'upload_dir': './uploads',
                'max_file_size': '104857600',
                'allowed_extensions': '.xlsx,.xls,.jpg,.jpeg,.png,.bmp',
            },
            'security': {
                'secret_key': 'NQI_PROJECT_DQY',
            },
            'logging': {
                'log_dir': './logs',
                'log_level': 'INFO',
                'log_rotation': '10 MB',
                'log_retention': '30 days',
            },
            'three_phase_meter': {
                'image_compression_enabled': 'true',
                'image_quality': '85',
                'image_max_size': '1048576',
            },
            'processing': {
                'poll_interval': '0.2',
                'supervisor_interval': '5',
                'heartbeat_timeout': '120',
                'error_backoff_max': '30',
                'status_log_interval': '60',
            },
        }

    def _path_from_config(self, section: str, option: str, fallback: str) -> Path:
        """读取路径配置；相对路径统一按项目根目录解析。"""
        value = self.config.get(section, option, fallback=fallback)
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.base_dir / path

    @property
    def db_host(self) -> str:
        return self.config.get('database', 'host')

    @property
    def db_port(self) -> int:
        return self.config.getint('database', 'port')

    @property
    def db_user(self) -> str:
        return self.config.get('database', 'user')

    @property
    def db_password(self) -> str:
        return self.config.get('database', 'password')

    @property
    def db_name(self) -> str:
        return self.config.get('database', 'database')

    @property
    def db_pool_size(self) -> int:
        return self.config.getint('database', 'pool_size', fallback=20)

    @property
    def db_max_overflow(self) -> int:
        return self.config.getint('database', 'max_overflow', fallback=40)

    @property
    def db_pool_timeout(self) -> int:
        return self.config.getint('database', 'pool_timeout', fallback=10)

    @property
    def db_pool_recycle(self) -> int:
        return self.config.getint('database', 'pool_recycle', fallback=1800)

    @property
    def server_host(self) -> str:
        return self.config.get('server', 'host')

    @property
    def server_port(self) -> int:
        return self.config.getint('server', 'port')

    @property
    def upload_dir(self) -> Path:
        return self._path_from_config('server', 'upload_dir', './uploads')

    @property
    def max_file_size(self) -> int:
        return self.config.getint('server', 'max_file_size')

    @property
    def allowed_extensions(self) -> List[str]:
        return self.config.get('server', 'allowed_extensions').split(',')

    @property
    def secret_key(self) -> str:
        return self.config.get('security', 'secret_key')

    @property
    def log_dir(self) -> Path:
        return self._path_from_config('logging', 'log_dir', './logs')

    @property
    def log_level(self) -> str:
        return self.config.get('logging', 'log_level', fallback='INFO')

    @property
    def processing_poll_interval(self) -> float:
        return max(0.1, self.config.getfloat('processing', 'poll_interval', fallback=0.2))

    @property
    def processing_supervisor_interval(self) -> float:
        return max(0.5, self.config.getfloat('processing', 'supervisor_interval', fallback=5.0))

    @property
    def processing_heartbeat_timeout(self) -> float:
        return max(10.0, self.config.getfloat('processing', 'heartbeat_timeout', fallback=120.0))

    @property
    def processing_error_backoff_max(self) -> float:
        return max(1.0, self.config.getfloat('processing', 'error_backoff_max', fallback=30.0))

    @property
    def processing_status_log_interval(self) -> float:
        return max(10.0, self.config.getfloat('processing', 'status_log_interval', fallback=60.0))

    # 三相表配置
    @property
    def image_compression_enabled(self) -> bool:
        return self.config.getboolean('three_phase_meter', 'image_compression_enabled', fallback=True)

    @property
    def image_quality(self) -> int:
        return self.config.getint('three_phase_meter', 'image_quality', fallback=85)

    @property
    def image_max_size(self) -> int:
        return self.config.getint('three_phase_meter', 'image_max_size', fallback=1048576)


config = Config()