from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, BigInteger, Enum, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from app.config_ini import config
from app.logger import logger
import enum

# 数据库连接字符串
DATABASE_URL = f"mysql+pymysql://{config.db_user}:{config.db_password}@{config.db_host}:{config.db_port}/{config.db_name}?charset=utf8mb4"

engine = create_engine(DATABASE_URL, pool_pre_ping=True, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class DataType(str, enum.Enum):
    """数据类型"""
    EXCEL = "excel"  # 电量数据
    IMAGE = "image"  # 几何量数据


class DeviceStatus(str, enum.Enum):
    """设备状态"""
    ONLINE = "online"
    OFFLINE = "offline"

# ==================== 硬件码表 ====================
class Hardware_Key(Base):
    __tablename__ = "hardware_key"

    id = Column(Integer, primary_key=True, index=True)
    hardware_key = Column(String(500), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ==================== 设备表 ====================
class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), unique=True, index=True, nullable=False)
    device_name = Column(String(200))
    device_ip = Column(String(50))
    hardware_key = Column(String(500), nullable=False)
    status = Column(String(20), default="offline")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # # 三相表专用字段
    # meter_model = Column(String(100))  # 表型号
    # meter_sn = Column(String(100))  # 表序列号
    # location = Column(String(200))  # 安装位置
    # description = Column(Text)  # 备注


# ==================== 电量数据表 ====================
class MeterExcelData(Base):
    """电量数据 - Excel文件记录（仅存储文件信息，不解析数据）"""
    __tablename__ = "meter_excel_data"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    file_name = Column(String(500), nullable=False)
    file_path = Column(Text, nullable=False)
    file_size = Column(BigInteger)
    upload_time = Column(DateTime, default=datetime.now, index=True)
    description = Column(Text)


# ==================== 几何量数据表 ====================
class MeterImageData(Base):
    """几何量数据 - 图片文件记录"""
    __tablename__ = "meter_image_data"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    file_name = Column(String(500), nullable=False)
    file_path = Column(Text, nullable=False)
    file_size = Column(BigInteger)
    original_size = Column(BigInteger)  # 原始大小（压缩前）
    upload_time = Column(DateTime, default=datetime.now, index=True)
    description = Column(Text)

    # 几何量数据特定字段
    image_type = Column(String(50))  # 图片类型（表盘/显示屏等）
    compression_ratio = Column(Float)  # 压缩比例





# ==================== 数据统计表 ====================
class DataStatistics(Base):
    """数据统计"""
    __tablename__ = "data_statistics"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    date = Column(DateTime, default=datetime.now, index=True)

    # 电量数据统计
    excel_count = Column(Integer, default=0)
    excel_total_size = Column(BigInteger, default=0)

    # 几何量数据统计
    image_count = Column(Integer, default=0)
    image_total_size = Column(BigInteger, default=0)
    image_original_size = Column(BigInteger, default=0)  # 原始大小

    # 更新时间
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ==================== 通知表 ====================
class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True)
    notification_type = Column(String(50))  # excel_upload, image_upload
    message = Column(Text)
    status = Column(String(20), default="unread")  # unread, read
    created_at = Column(DateTime, default=datetime.now, index=True)
    read_at = Column(DateTime)


def init_db():
    """初始化数据库"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()