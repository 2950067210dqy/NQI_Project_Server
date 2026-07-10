from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, BigInteger, Enum, Float, Boolean, inspect, text
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
class DeviceRegistrationRequest(Base):
    __tablename__ = "device_registration_requests"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    device_name = Column(String(200))
    device_ip = Column(String(50))
    location = Column(String(50), index=True)
    hardware_key = Column(String(500), nullable=False)
    status = Column(String(20), default="pending", index=True)  # pending/approved/rejected
    review_message = Column(Text)
    requested_at = Column(DateTime, default=datetime.now, index=True)
    reviewed_at = Column(DateTime)


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), unique=True, index=True, nullable=False)
    device_name = Column(String(200))
    device_ip = Column(String(50))
    location = Column(String(50), index=True)
    hardware_key = Column(String(500), nullable=False)
    status = Column(String(20), default="offline")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ==================== 电量数据表 ====================
class MeterExcelData(Base):
    """电量数据文件主表。"""
    __tablename__ = "meter_excel_data"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    file_name = Column(String(500), nullable=False)
    file_path = Column(Text, nullable=False)
    file_size = Column(BigInteger)
    location = Column(String(50), index=True)
    upload_time = Column(DateTime, default=datetime.now, index=True)
    description = Column(Text)
    processing_status = Column(String(20), default="pending", index=True)  # pending/processing/done/failed
    processing_error = Column(Text)
    processed_at = Column(DateTime)


# ==================== 几何量数据表 ====================
class MeterImageData(Base):
    """几何量数据文件主表。"""
    __tablename__ = "meter_image_data"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    file_name = Column(String(500), nullable=False)
    file_path = Column(Text, nullable=False)
    file_size = Column(BigInteger)
    original_size = Column(BigInteger)  # 原始大小（压缩前）
    location = Column(String(50), index=True)
    upload_time = Column(DateTime, default=datetime.now, index=True)
    description = Column(Text)

    image_type = Column(String(50))
    compression_ratio = Column(Float)
    processing_status = Column(String(20), default="pending", index=True)  # pending/processing/done/failed
    processing_error = Column(Text)
    processed_at = Column(DateTime)


class MeterExcelParseResult(Base):
    """电量数据解析结果表。"""
    __tablename__ = "meter_excel_parse_results"

    id = Column(Integer, primary_key=True, index=True)
    excel_id = Column(Integer, unique=True, index=True, nullable=False)
    device_id = Column(String(100), index=True, nullable=False)
    sheet_count = Column(Integer, default=0)
    rated_voltage = Column(Float, default=0)
    rated_voltage_unit = Column(String(20), default="")
    rated_frequency = Column(Float, default=0)
    rated_frequency_unit = Column(String(20), default="")
    numeric_value_count = Column(Integer, default=0)
    max_numeric_value = Column(Float, default=0)
    min_numeric_value = Column(Float, default=0)
    avg_numeric_value = Column(Float, default=0)
    parse_summary = Column(Text)
    parsed_data_json = Column(Text)
    parser_version = Column(String(50), default="server_v1")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    processed_at = Column(DateTime, default=datetime.now, index=True)


class MeterImageAnalysisResult(Base):
    """几何量图片分析结果表。"""
    __tablename__ = "meter_image_analysis_results"

    id = Column(Integer, primary_key=True, index=True)
    image_id = Column(Integer, unique=True, index=True, nullable=False)
    device_id = Column(String(100), index=True, nullable=False)
    recognized_path = Column(Text)
    image_width = Column(Integer, default=0)
    image_height = Column(Integer, default=0)
    image_mode = Column(String(50), default="")
    mean_brightness = Column(Float, default=0)
    brightness_std = Column(Float, default=0)
    contrast_score = Column(Float, default=0)
    sharpness_score = Column(Float, default=0)
    dominant_color = Column(String(50), default="")
    has_fault = Column(Boolean, default=False, index=True)
    analysis_summary = Column(Text)
    analysis_data_json = Column(Text)
    analyzer_version = Column(String(50), default="server_v1")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    processed_at = Column(DateTime, default=datetime.now, index=True)


# ==================== 数据统计表 ====================
class FaultRecord(Base):
    __tablename__ = "fault_records"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    data_type = Column(String(20), index=True, nullable=False)
    file_id = Column(Integer, index=True, nullable=False)
    fault_type = Column(String(100), default="数据异常")
    severity = Column(String(20), default="warning")
    message = Column(Text)
    source = Column(String(50), default="auto")
    status = Column(String(20), default="open", index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    resolved_at = Column(DateTime)


class DataSearchIndex(Base):
    __tablename__ = "data_search_index"

    id = Column(Integer, primary_key=True, index=True)
    data_type = Column(String(20), index=True, nullable=False)
    file_id = Column(Integer, index=True, nullable=False)
    device_id = Column(String(100), index=True, nullable=False)
    file_name = Column(String(500), nullable=False)
    location = Column(String(50), index=True)
    has_fault = Column(Boolean, default=False, index=True)
    fault_summary = Column(Text)
    occurred_at = Column(DateTime, index=True)
    uploaded_at = Column(DateTime, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)


class DataStatistics(Base):
    """数据统计"""
    __tablename__ = "data_statistics"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True, nullable=False)
    date = Column(DateTime, default=datetime.now, index=True)
    excel_count = Column(Integer, default=0)
    excel_total_size = Column(BigInteger, default=0)
    image_count = Column(Integer, default=0)
    image_total_size = Column(BigInteger, default=0)
    image_original_size = Column(BigInteger, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AlarmRule(Base):
    """预警规则表，供上位机配置电量/几何量阈值和上传异常告警。"""
    __tablename__ = "alarm_rules"

    id = Column(Integer, primary_key=True, index=True)
    rule_name = Column(String(200), nullable=False)
    data_type = Column(String(20), index=True, nullable=False)
    metric_key = Column(String(100), index=True, nullable=False)
    operator = Column(String(20), default="gt")
    threshold_value = Column(Float)
    enabled = Column(Boolean, default=True, index=True)
    severity = Column(String(20), default="warning")
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), index=True)
    notification_type = Column(String(50))
    message = Column(Text)
    status = Column(String(20), default="unread")
    created_at = Column(DateTime, default=datetime.now, index=True)
    read_at = Column(DateTime)


def init_db():
    """初始化数据库"""
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_legacy_columns()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def _ensure_legacy_columns():
    """为已存在的旧表补齐本轮新增列，避免手工迁移。"""
    inspector = inspect(engine)
    table_columns = {
        "device_registration_requests": {
            "location": "ALTER TABLE device_registration_requests ADD COLUMN location VARCHAR(50) NULL",
        },
        "devices": {
            "location": "ALTER TABLE devices ADD COLUMN location VARCHAR(50) NULL",
        },
        "meter_excel_data": {
            "location": "ALTER TABLE meter_excel_data ADD COLUMN location VARCHAR(50) NULL",
            "processing_status": "ALTER TABLE meter_excel_data ADD COLUMN processing_status VARCHAR(20) DEFAULT 'pending'",
            "processing_error": "ALTER TABLE meter_excel_data ADD COLUMN processing_error TEXT NULL",
            "processed_at": "ALTER TABLE meter_excel_data ADD COLUMN processed_at DATETIME NULL",
        },
        "meter_image_data": {
            "location": "ALTER TABLE meter_image_data ADD COLUMN location VARCHAR(50) NULL",
            "processing_status": "ALTER TABLE meter_image_data ADD COLUMN processing_status VARCHAR(20) DEFAULT 'pending'",
            "processing_error": "ALTER TABLE meter_image_data ADD COLUMN processing_error TEXT NULL",
            "processed_at": "ALTER TABLE meter_image_data ADD COLUMN processed_at DATETIME NULL",
        },
    }

    with engine.begin() as connection:
        for table_name, columns in table_columns.items():
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, ddl in columns.items():
                if column_name in existing:
                    continue
                logger.info(f"Applying schema patch: {table_name}.{column_name}")
                connection.execute(text(ddl))


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
