from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List
from enum import Enum

class DataType(str, Enum):
    """数据类型"""
    EXCEL = "excel"
    IMAGE = "image"

# ==================== 设备相关 ====================
class DeviceBase(BaseModel):
    device_id: str
    device_name: str
    device_ip: Optional[str] = None
    meter_model: Optional[str] = None
    meter_sn: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None

class DeviceCreate(DeviceBase):
    hardware_key: str

class DeviceAuthenticate(BaseModel):
    device_id: str
    hardware_key: str
    device_ip: Optional[str] = None
    location: Optional[str] = None

class DeviceResponse(BaseModel):
    device_id: str
    device_name: str
    device_ip: Optional[str]
    status: str
    meter_model: Optional[str]
    meter_sn: Optional[str]
    location: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

# ==================== 电量数据相关 ====================
class MeterExcelDataBase(BaseModel):
    file_name: str
    description: Optional[str] = None
    measurement_date: Optional[datetime] = None
    meter_reading: Optional[float] = None
    total_energy: Optional[float] = None
    a_phase_voltage: Optional[float] = None
    b_phase_voltage: Optional[float] = None
    c_phase_voltage: Optional[float] = None
    a_phase_current: Optional[float] = None
    b_phase_current: Optional[float] = None
    c_phase_current: Optional[float] = None
    power_factor: Optional[float] = None

class MeterExcelDataCreate(MeterExcelDataBase):
    device_id: str
    file_path: str
    file_size: int

class MeterExcelDataResponse(MeterExcelDataBase):
    id: int
    device_id: str
    file_path: str
    file_size: int
    upload_time: datetime

    class Config:
        from_attributes = True

# ==================== 几何量数据相关 ====================
class MeterImageDataBase(BaseModel):
    file_name: str
    description: Optional[str] = None
    image_type: Optional[str] = None

class MeterImageDataCreate(MeterImageDataBase):
    device_id: str
    file_path: str
    file_size: int
    original_size: Optional[int] = None

class MeterImageDataResponse(MeterImageDataBase):
    id: int
    device_id: str
    file_path: str
    file_size: int
    original_size: Optional[int]
    compression_ratio: Optional[float]
    upload_time: datetime

    class Config:
        from_attributes = True

# ==================== 通知相关 ====================
class NotificationResponse(BaseModel):
    id: int
    device_id: str
    notification_type: str
    message: str
    created_at: datetime

    class Config:
        from_attributes = True

class AdminPasswordVerify(BaseModel):
    """上位机启动时提交的管理员密码校验请求。"""
    password: str


# ==================== 统计相关 ====================
class DataStatisticsResponse(BaseModel):
    device_id: str
    date: datetime
    excel_count: int
    excel_total_size: int
    image_count: int
    image_total_size: int
    image_original_size: Optional[int]
    compression_saved: Optional[int]  # 压缩节省的大小

    class Config:
        from_attributes = True