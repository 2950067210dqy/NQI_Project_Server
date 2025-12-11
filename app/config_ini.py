import configparser
from pathlib import Path
from typing import List


class Config:
    def __init__(self, config_file: str = "config.ini"):
        self.config = configparser.ConfigParser()
        self.config.read(config_file, encoding='utf-8')

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
    def server_host(self) -> str:
        return self.config.get('server', 'host')

    @property
    def server_port(self) -> int:
        return self.config.getint('server', 'port')

    @property
    def upload_dir(self) -> Path:
        return Path(self.config.get('server', 'upload_dir'))

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
        return Path(self.config.get('logging', 'log_dir'))

    @property
    def log_level(self) -> str:
        return self.config.get('logging', 'log_level')

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