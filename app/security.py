from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend
import base64
import hashlib
from app.logger import logger


class SecurityManager:
    """安全管理器"""

    @staticmethod
    def generate_hardware_key(unique_info: str) -> str:
        """
        生成硬件密钥
        unique_info: 设备唯一信息（如MAC地址、CPU序列号等）
        """
        hash_obj = hashlib.sha256(unique_info.encode())
        return hash_obj.hexdigest()

    @staticmethod
    def verify_hardware_key(provided_key: str, stored_key: str) -> bool:
        """验证硬件密钥"""
        return provided_key == stored_key

    @staticmethod
    def encrypt_data(data: bytes, password: str) -> bytes:
        """加密数据"""
        try:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'static_salt_change_this',
                iterations=100000,
                backend=default_backend()
            )
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
            f = Fernet(key)
            return f.encrypt(data)
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise

    @staticmethod
    def decrypt_data(encrypted_data: bytes, password: str) -> bytes:
        """解密数据"""
        try:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'static_salt_change_this',
                iterations=100000,
                backend=default_backend()
            )
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
            f = Fernet(key)
            return f.decrypt(encrypted_data)
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise


security_manager = SecurityManager()