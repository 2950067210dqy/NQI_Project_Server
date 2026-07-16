from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, BigInteger, Enum, Float, Boolean, inspect, text, Index, CheckConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.mysql import LONGTEXT
from datetime import datetime
from app.config_ini import config
from app.logger import logger
import enum

# 数据库连接字符串
DATABASE_URL = f"mysql+pymysql://{config.db_user}:{config.db_password}@{config.db_host}:{config.db_port}/{config.db_name}?charset=utf8mb4"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=config.db_pool_size,
    max_overflow=config.db_max_overflow,
    pool_timeout=config.db_pool_timeout,
    pool_recycle=config.db_pool_recycle,
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
logger.info(
    f"Database pool initialized: size={config.db_pool_size}, "
    f"overflow={config.db_max_overflow}, timeout={config.db_pool_timeout}s, "
    f"recycle={config.db_pool_recycle}s"
)
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


class DeviceIdReservation(Base):
    """注册阶段的设备 ID 唯一占用记录，用数据库主键阻止并发重复注册。"""
    __tablename__ = "device_id_reservations"

    # 使用规范化的小写 ID 作为主键，让 E001 与 e001 也被视为同一个设备编号。
    device_id = Column(String(100), primary_key=True)
    hardware_key = Column(String(500), nullable=False)
    request_id = Column(Integer, index=True)
    status = Column(String(20), default="pending", index=True)  # pending/approved
    claimed_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


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
    chart_point_count = Column(Integer, default=0)
    chart_value_count = Column(Integer, default=0)
    error_value_count = Column(Integer, default=0)
    parse_summary = Column(LONGTEXT)
    parsed_data_json = Column(LONGTEXT)
    detail_summary_json = Column(LONGTEXT)
    parser_version = Column(String(50), default="server_v1")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    processed_at = Column(DateTime, default=datetime.now, index=True)


class MeterExcelMeasurementDetail(Base):
    """电量数据解析明细表：按 Sheet/指标/相位/设备/测试点拆分数值，供精细化预警和查询使用。"""
    __tablename__ = "meter_excel_measurement_details"

    id = Column(Integer, primary_key=True, index=True)
    excel_id = Column(Integer, index=True, nullable=False)
    parse_result_id = Column(Integer, index=True, nullable=False)
    device_id = Column(String(100), index=True, nullable=False)
    sheet_name = Column(String(100), index=True, nullable=False)
    metric_group_index = Column(Integer, default=0, index=True)
    metric_name = Column(String(50), index=True, nullable=False)
    metric_key = Column(String(50), index=True, nullable=False)
    phase_name = Column(String(50), index=True, nullable=False)
    phase_index = Column(Integer, default=0)
    meter_name = Column(String(100), index=True, nullable=False)
    meter_index = Column(Integer, default=0)
    point_index = Column(Integer, index=True, nullable=False)
    source_excel_row = Column(Integer, index=True)
    range_text = Column(String(100))
    frequency_hz = Column(Float)
    rated_voltage_v = Column(Float)
    rated_current_a = Column(Float)
    x_angle_degree = Column(Float)
    x_current_a = Column(Float)
    value_unit = Column(String(30))
    chart_series_name = Column(String(200))
    value = Column(Float, index=True)
    created_at = Column(DateTime, default=datetime.now)
    processed_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index("idx_excel_detail_lookup", "excel_id", "metric_key", "sheet_name", "phase_name", "meter_name"),
    )



class MeterExcelErrorDetail(Base):
    """电量 Excel 误差明细表：保存每个 Sheet/指标/相位/测试点的百分比误差和 ppm 误差。"""
    __tablename__ = "meter_excel_error_details"

    id = Column(Integer, primary_key=True, index=True)
    excel_id = Column(Integer, index=True, nullable=False)
    parse_result_id = Column(Integer, index=True, nullable=False)
    device_id = Column(String(100), index=True, nullable=False)
    sheet_name = Column(String(100), index=True, nullable=False)
    metric_group_index = Column(Integer, default=0, index=True)
    metric_name = Column(String(50), index=True, nullable=False)
    metric_key = Column(String(50), index=True, nullable=False)
    phase_name = Column(String(50), index=True, nullable=False)
    phase_index = Column(Integer, default=0)
    point_index = Column(Integer, index=True, nullable=False)
    source_excel_row = Column(Integer, index=True)
    range_text = Column(String(100))
    frequency_hz = Column(Float)
    rated_voltage_v = Column(Float)
    rated_current_a = Column(Float)
    x_angle_degree = Column(Float)
    x_current_a = Column(Float)
    reference_meter_name = Column(String(100))
    compared_meter_name = Column(String(100))
    reference_value = Column(Float)
    compared_value = Column(Float)
    error_percent = Column(Float, index=True)
    error_ppm = Column(Float, index=True)
    created_at = Column(DateTime, default=datetime.now)
    processed_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index("idx_excel_error_lookup", "excel_id", "metric_key", "sheet_name", "phase_name"),
    )

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
    # 预警通知直接关联原始文件，供上位机历史页查看和下载。
    data_type = Column(String(20), index=True)
    file_id = Column(Integer, index=True)
    file_name = Column(String(255))
    message = Column(Text)
    status = Column(String(20), default="unread")
    created_at = Column(DateTime, default=datetime.now, index=True)
    read_at = Column(DateTime)


class SystemAdminCredential(Base):
    """上位机管理员唯一凭据；只允许数据库管理员直接修改 password 字段。"""
    __tablename__ = "system_admin_credentials"

    id = Column(Integer, primary_key=True, default=1)
    singleton_key = Column(String(50), nullable=False, unique=True, default="upper_client_admin")
    password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_system_admin_credentials_singleton"),
    )


def init_db():
    """初始化数据库"""
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_legacy_columns()
        _ensure_system_admin_credential()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def _ensure_system_admin_credential():
    """仅在凭据不存在时写入默认密码，绝不覆盖数据库中的人工修改。"""
    db = SessionLocal()
    try:
        credential = db.query(SystemAdminCredential).filter(
            SystemAdminCredential.singleton_key == "upper_client_admin"
        ).first()
        if credential is None:
            db.add(SystemAdminCredential(
                id=1,
                singleton_key="upper_client_admin",
                password="nqisystemadmin",
            ))
            db.commit()
            logger.info("Default upper-client administrator credential initialized")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
        "notifications": {
            "data_type": "ALTER TABLE notifications ADD COLUMN data_type VARCHAR(20) NULL",
            "file_id": "ALTER TABLE notifications ADD COLUMN file_id INT NULL",
            "file_name": "ALTER TABLE notifications ADD COLUMN file_name VARCHAR(255) NULL",
        },
        "meter_excel_data": {
            "location": "ALTER TABLE meter_excel_data ADD COLUMN location VARCHAR(50) NULL",
            "processing_status": "ALTER TABLE meter_excel_data ADD COLUMN processing_status VARCHAR(20) DEFAULT 'pending'",
            "processing_error": "ALTER TABLE meter_excel_data ADD COLUMN processing_error TEXT NULL",
            "processed_at": "ALTER TABLE meter_excel_data ADD COLUMN processed_at DATETIME NULL",
        },
        "meter_excel_parse_results": {
            "chart_point_count": "ALTER TABLE meter_excel_parse_results ADD COLUMN chart_point_count INT DEFAULT 0",
            "chart_value_count": "ALTER TABLE meter_excel_parse_results ADD COLUMN chart_value_count INT DEFAULT 0",
            "error_value_count": "ALTER TABLE meter_excel_parse_results ADD COLUMN error_value_count INT DEFAULT 0",
            "detail_summary_json": "ALTER TABLE meter_excel_parse_results ADD COLUMN detail_summary_json LONGTEXT NULL",
        },
        "meter_excel_measurement_details": {
            "metric_group_index": "ALTER TABLE meter_excel_measurement_details ADD COLUMN metric_group_index INT DEFAULT 0",
            "phase_index": "ALTER TABLE meter_excel_measurement_details ADD COLUMN phase_index INT DEFAULT 0",
            "meter_index": "ALTER TABLE meter_excel_measurement_details ADD COLUMN meter_index INT DEFAULT 0",
            "source_excel_row": "ALTER TABLE meter_excel_measurement_details ADD COLUMN source_excel_row INT NULL",
            "range_text": "ALTER TABLE meter_excel_measurement_details ADD COLUMN range_text VARCHAR(100) NULL",
            "frequency_hz": "ALTER TABLE meter_excel_measurement_details ADD COLUMN frequency_hz FLOAT NULL",
            "rated_voltage_v": "ALTER TABLE meter_excel_measurement_details ADD COLUMN rated_voltage_v FLOAT NULL",
            "rated_current_a": "ALTER TABLE meter_excel_measurement_details ADD COLUMN rated_current_a FLOAT NULL",
            "value_unit": "ALTER TABLE meter_excel_measurement_details ADD COLUMN value_unit VARCHAR(30) NULL",
            "chart_series_name": "ALTER TABLE meter_excel_measurement_details ADD COLUMN chart_series_name VARCHAR(200) NULL",
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

        if engine.dialect.name == "mysql":
            # MySQL TEXT 最大约 64KB，电量 Excel 的图表 JSON 和明细摘要会超过该限制。
            for ddl in (
                "ALTER TABLE meter_excel_parse_results MODIFY parse_summary LONGTEXT NULL",
                "ALTER TABLE meter_excel_parse_results MODIFY parsed_data_json LONGTEXT NULL",
                "ALTER TABLE meter_excel_parse_results MODIFY detail_summary_json LONGTEXT NULL",
            ):
                logger.info(f"Applying schema type patch: {ddl}")
                connection.execute(text(ddl))

def get_db():
    """获取数据库会话；接口异常时回滚并确保连接归还连接池。"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
