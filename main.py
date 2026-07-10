from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import Optional, List
import aiofiles
from pathlib import Path
from datetime import datetime, timedelta
import shutil
import asyncio

from app.config_ini import config
from app.logger import logger
from app.database import init_db, get_db, SessionLocal, Device, MeterExcelData, MeterImageData, Notification, DataStatistics, \
    DataType, Hardware_Key, DeviceRegistrationRequest, FaultRecord, DataSearchIndex
from app.security import security_manager
from app.utils import image_compressor
from app.meter_utils import meter_image_classifier
from app.schemas import (
    DeviceCreate, DeviceAuthenticate, DeviceResponse,
    MeterExcelDataResponse, MeterImageDataResponse,
    NotificationResponse, DataStatisticsResponse
)
from app.feature_routes import router as feature_router
from app.feature_services import (
    detect_fault_flag, create_search_index, create_fault_if_needed,
    remember_polling_notification, create_alarm_notification, extract_excel_metrics,
    build_image_metrics, evaluate_alarm_rules, merge_fault_summary, create_upload_error_notification,
    infer_location
)
from app.server_processing_runtime import (
    start_processing_workers,
    stop_processing_workers,
    serialize_excel_record,
    serialize_image_record,
)

app = FastAPI(title="三相表数据管理系统")
app.mount("/uploads", StaticFiles(directory=str(config.upload_dir.resolve()), check_dir=False), name="uploads")
app.include_router(feature_router)


@app.on_event("startup")
async def startup_event():
    """启动事件"""
    init_db()
    config.upload_dir.mkdir(parents=True, exist_ok=True)
    # 启动两个后台处理线程，持续扫描待解析的 Excel 与图片记录。
    start_processing_workers()
    logger.info("Three-Phase Meter Server started successfully")


@app.on_event("shutdown")
async def shutdown_event():
    """关闭事件"""
    # 关闭服务时优雅停止后台解析线程，避免残留轮询任务。
    stop_processing_workers()
    logger.info("Server shutting down")


# ==================== 设备管理接口 ====================

@app.post("/api/device/register")
async def register_device(
        device_id: str = Form(...),
        device_name: str = Form(...),
        hardware_key: str = Form(...),
        device_ip: str = Form(None),
        location: str = Form(None),

        db: Session = Depends(get_db)
):
    """提交设备注册申请；只有审批通过后设备才可认证连接。"""
    try:
        # 已经通过审批并落库的设备，重复注册时直接返回已审批状态。
        existing_device = db.query(Device).filter(Device.device_id == device_id).first()
        if existing_device:
            if security_manager.verify_hardware_key(hardware_key, existing_device.hardware_key):
                # 已审批设备再次注册时，同步最新 IP 和城市，便于上位机查看在线位置。
                existing_device.device_ip = device_ip or existing_device.device_ip
                existing_device.location = location or existing_device.location or infer_location(device_id)
                existing_device.updated_at = datetime.now()
                db.commit()
                logger.info(f"设备已审批，可直接连接服务器: {device_id}")
                return {
                    "status": "approved",
                    "message": "Device already approved, you can connect to server directly",
                    "device_id": device_id
                }
            raise HTTPException(status_code=400, detail="Device ID already exists with another hardware key")

        # 若已有待审批申请，则只更新申请信息，避免同一设备重复堆积多条 pending。
        pending_request = db.query(DeviceRegistrationRequest).filter(
            DeviceRegistrationRequest.device_id == device_id,
            DeviceRegistrationRequest.hardware_key == hardware_key,
            DeviceRegistrationRequest.status == "pending"
        ).first()

        if pending_request:
            pending_request.device_name = device_name
            pending_request.device_ip = device_ip
            pending_request.location = location or pending_request.location or infer_location(device_id)
            pending_request.requested_at = datetime.now()
            db.commit()
            db.refresh(pending_request)
            request_record = pending_request
        else:
            request_record = DeviceRegistrationRequest(
                device_id=device_id,
                device_name=device_name,
                device_ip=device_ip,
                location=location or infer_location(device_id),
                hardware_key=hardware_key,
                status="pending"
            )
            db.add(request_record)
            db.commit()
            db.refresh(request_record)

        db.add(Notification(
            device_id=device_id,
            notification_type="device_register_request",
            message=f"设备注册申请: {device_id} - {device_name} - {location or infer_location(device_id)}",
            status="unread"
        ))
        db.commit()

        # 上位机通过同一条 HTTP 长轮询通道收到新的注册审批事项。
        remember_polling_notification({
            "type": "device_register_request",
            "device_id": device_id,
            "file_name": device_name,
            "file_size": 0,
            "data_type": "设备管理",
            "request_id": request_record.id,
            "timestamp": datetime.now().isoformat()
        })

        logger.info(f"设备注册申请已提交，等待审批: {device_id}")
        return {
            "status": "pending",
            "message": "Registration request submitted, waiting for approval",
            "request_id": request_record.id,
            "device_id": device_id,
            "location": request_record.location
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Device registration failed: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/device/authenticate")
async def authenticate_device(
        device_id: str = Form(...),
        hardware_key: str = Form(...),
        device_ip: str = Form(None),
        location: str = Form(None),
        db: Session = Depends(get_db)
):
    """设备认证"""
    try:
        device = db.query(Device).filter(Device.device_id == device_id).first()

        if not device:
            logger.warning(f"设备认证失败: 设备不存在 - {device_id}")
            raise HTTPException(status_code=404, detail="Device not found")

        # 验证硬件密钥
        if not security_manager.verify_hardware_key(hardware_key, device.hardware_key):
            logger.warning(f"设备认证失败: 硬件密钥错误 - {device_id}")
            raise HTTPException(status_code=401, detail="Invalid hardware key")

        # 认证成功后更新在线状态、IP 和城市，城市为空时沿用旧值或按设备号兜底推断。
        device.status = "online"
        device.device_ip = device_ip
        device.location = location or device.location or infer_location(device_id)
        device.updated_at = datetime.now()
        db.commit()

        logger.info(f"三相表设备认证成功: {device_id}")

        return {
            "status": "success",
            "message": "Authentication successful",
            "device_id": device_id,
            "device_name": device.device_name,
            "location": device.location
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 通用文件上传接口 ====================

@app.post("/api/upload/file")
async def upload_file(
        background_tasks: BackgroundTasks,
        device_id: str = Form(...),
        hardware_key: str = Form(...),
        description: str = Form(None),
        meter_model: str = Form(None),
        meter_sn: str = Form(None),
        image_type: str = Form(None),
        location: str = Form(None),
        has_fault: Optional[bool] = Form(None),
        file: UploadFile = File(...),
        db: Session = Depends(get_db)
):
    """
    通用文件上传接口
    根据文件类型自动路由到相应的处理函数

    支持的文件类型:
    - Excel: .xlsx, .xls (电量数据)
    - Image: .jpg, .jpeg, .png, .bmp (几何量数据)
    """
    try:
        # 检查文件扩展名
        file_ext = Path(file.filename).suffix.lower()

        # 判断文件类型
        if file_ext in ['.xlsx', '.xls']:
            logger.info(f"检测到电量数据(Excel): {file.filename}")
            # 路由到 Excel 上传处理
            return await upload_excel_data(
                background_tasks=background_tasks,
                device_id=device_id,
                hardware_key=hardware_key,
                description=description,
                meter_model=meter_model,
                meter_sn=meter_sn,
                file=file,
                location=location,
                has_fault=has_fault,
                db=db
            )

        elif file_ext in ['.jpg', '.jpeg', '.png', '.bmp']:
            logger.info(f"检测到几何量数据(Image): {file.filename}")
            # 路由到 Image 上传处理
            return await upload_image_data(
                background_tasks=background_tasks,
                device_id=device_id,
                hardware_key=hardware_key,
                description=description,
                image_type=image_type,
                file=file,
                location=location,
                has_fault=has_fault,
                db=db
            )

        else:
            logger.warning(f"不支持的文件类型: {file_ext}")
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {file_ext}. Allowed: .xlsx, .xls, .jpg, .jpeg, .png, .bmp"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"File upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== HTTP长轮询通知函数 ====================

async def send_polling_notification(notification_type: str, device_id: str, file_info: dict):
    """写入HTTP长轮询通知队列，供上位机通过 /api/polling/notifications 拉取。"""
    try:
        payload = {
            "type": notification_type,
            "device_id": device_id,
            "file_id": file_info.get("file_id"),
            "file_name": file_info.get("file_name"),
            "file_size": file_info.get("file_size"),
            "data_type": file_info.get("data_type", ""),
            "timestamp": datetime.now().isoformat()
        }
        
        # 如果是图片，添加额外信息
        if notification_type == "image_upload":
            payload["original_size"] = file_info.get("original_size")
            payload["compression_ratio"] = file_info.get("compression_ratio")

        # 服务器环境不支持WebSocket，所有实时事件统一进入HTTP长轮询队列。
        remember_polling_notification(payload)
        logger.info(f"HTTP长轮询通知已写入: {notification_type} - {device_id} - {file_info.get('file_name')}")
    except Exception as e:
        logger.error(f"Failed to create polling notification: {e}")


# ==================== 电量数据接口 ====================


async def upload_excel_data(
        background_tasks: BackgroundTasks,
        device_id: str,
        hardware_key: str,
        description: str,
        meter_model: str,
        meter_sn: str,
        file: UploadFile,
        location: str,
        has_fault: Optional[bool],
        db: Session
):
    """上传电量数据（Excel）- 文件先落盘，再由服务端后台线程解析。"""
    try:
        device = db.query(Device).filter(Device.device_id == device_id).first()
        if not device or not security_manager.verify_hardware_key(hardware_key, device.hardware_key):
            raise HTTPException(status_code=401, detail="Authentication failed")

        file_ext = Path(file.filename).suffix.lower()
        if file_ext not in ['.xlsx', '.xls']:
            raise HTTPException(status_code=400, detail=f"File type not allowed: {file_ext}")

        device_dir = config.upload_dir / device_id / "excel"
        device_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{file.filename}"
        file_path = device_dir / filename

        file_content = await file.read()
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(file_content)

        file_size = len(file_content)
        location_value = location or device.location or infer_location(device_id)
        if location:
            # 数据上传也携带城市，服务器同步刷新设备当前位置，方便上位机在线页查看。
            device.location = location_value
            device.updated_at = datetime.now()
        excel_record = MeterExcelData(
            device_id=device_id,
            file_name=filename,
            file_path=str(file_path),
            file_size=file_size,
            location=location_value,
            description=description or f"电量数据 - {timestamp}",
            processing_status="pending",
        )

        db.add(excel_record)
        db.commit()
        db.refresh(excel_record)

        update_statistics(db, device_id, "excel", file_size)
        detected_fault, fault_summary = detect_fault_flag(filename, description, has_fault)
        create_search_index(db, "excel", excel_record, location_value, detected_fault, fault_summary)

        notification = Notification(
            device_id=device_id,
            notification_type="excel_upload",
            message=f"电量数据上传: {filename}",
            status="unread"
        )
        db.add(notification)
        db.commit()

        file_info = {
            "file_id": excel_record.id,
            "file_name": filename,
            "file_size": file_size,
            "data_type": "电量数据",
            "processing_status": "pending",
        }
        background_tasks.add_task(send_polling_notification, "excel_upload", device_id, file_info)

        logger.info(f"电量数据上传成功: {device_id}/{filename} ({file_size} bytes)，等待服务端后台解析")
        return {
            "status": "success",
            "message": "Excel data uploaded successfully",
            "data_type": "excel",
            "file_id": excel_record.id,
            "file_name": filename,
            "file_size": file_size,
            "processing_status": "pending",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Excel upload failed: {e}")
        db.rollback()
        create_upload_error_notification(db, device_id, "excel", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/upload/excel")
async def upload_excel_endpoint(
        background_tasks: BackgroundTasks,
        device_id: str = Form(...),
        hardware_key: str = Form(...),
        description: str = Form(None),
        meter_model: str = Form(None),
        meter_sn: str = Form(None),
        file: UploadFile = File(...),
        location: str = Form(None),
        has_fault: Optional[bool] = Form(None),
        db: Session = Depends(get_db)
):
    """上传电量数据（Excel）- 直接调用接口"""
    return await upload_excel_data(
        background_tasks=background_tasks,
        device_id=device_id,
        hardware_key=hardware_key,
        description=description,
        meter_model=meter_model,
        meter_sn=meter_sn,
        file=file,
        location=location,
        has_fault=has_fault,
        db=db
    )


# ==================== 几何量数据接口 ====================

async def upload_image_data(
        background_tasks: BackgroundTasks,
        device_id: str,
        hardware_key: str,
        description: str,
        image_type: str,
        file: UploadFile,
        location: str,
        has_fault: Optional[bool],
        db: Session
):
    """上传几何量数据（图片）- 文件先落盘，再由服务端后台线程分析。"""
    try:
        device = db.query(Device).filter(Device.device_id == device_id).first()
        if not device or not security_manager.verify_hardware_key(hardware_key, device.hardware_key):
            raise HTTPException(status_code=401, detail="Authentication failed")

        file_ext = Path(file.filename).suffix.lower()
        if file_ext not in ['.jpg', '.jpeg', '.png', '.bmp']:
            raise HTTPException(status_code=400, detail=f"File type not allowed: {file_ext}")

        device_dir = config.upload_dir / device_id / "image"
        device_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{file.filename}"
        file_path = device_dir / filename

        file_content = await file.read()
        original_size = len(file_content)
        if config.image_compression_enabled:
            logger.info(f"Compressing image: {filename}")
            file_content = image_compressor.compress_image(
                file_content,
                quality=config.image_quality,
                max_size=config.image_max_size
            )

        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(file_content)

        compressed_size = len(file_content)
        compression_ratio = ((original_size - compressed_size) / original_size * 100) if original_size > 0 else 0
        if not image_type:
            image_type = meter_image_classifier.classify_image(filename)

        location_value = location or device.location or infer_location(device_id)
        if location:
            # 图片上传携带的城市和文件记录一起保存，并回写设备当前城市。
            device.location = location_value
            device.updated_at = datetime.now()
        image_record = MeterImageData(
            device_id=device_id,
            file_name=filename,
            file_path=str(file_path),
            file_size=compressed_size,
            original_size=original_size,
            location=location_value,
            description=description or f"几何量数据 - {timestamp}",
            image_type=image_type,
            compression_ratio=compression_ratio,
            processing_status="pending",
        )

        db.add(image_record)
        db.commit()
        db.refresh(image_record)

        update_statistics(db, device_id, "image", compressed_size, original_size)
        detected_fault, fault_summary = detect_fault_flag(filename, description, has_fault)
        create_search_index(db, "image", image_record, location_value, detected_fault, fault_summary)

        notification = Notification(
            device_id=device_id,
            notification_type="image_upload",
            message=f"几何量数据上传: {filename}",
            status="unread"
        )
        db.add(notification)
        db.commit()

        file_info = {
            "file_id": image_record.id,
            "file_name": filename,
            "file_size": compressed_size,
            "original_size": original_size,
            "compression_ratio": compression_ratio,
            "data_type": "几何量数据",
            "processing_status": "pending",
        }
        background_tasks.add_task(send_polling_notification, "image_upload", device_id, file_info)

        logger.info(
            f"几何量数据上传成功: {device_id}/{filename} "
            f"({original_size} -> {compressed_size} bytes, 压缩率: {compression_ratio:.1f}%)，等待服务端后台分析"
        )

        return {
            "status": "success",
            "message": "Image data uploaded successfully",
            "data_type": "image",
            "file_id": image_record.id,
            "file_name": filename,
            "original_size": original_size,
            "compressed_size": compressed_size,
            "compression_ratio": compression_ratio,
            "image_type": image_type,
            "processing_status": "pending",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image upload failed: {e}")
        db.rollback()
        create_upload_error_notification(db, device_id, "image", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/upload/image")
async def upload_image_endpoint(
        background_tasks: BackgroundTasks,
        device_id: str = Form(...),
        hardware_key: str = Form(...),
        description: str = Form(None),
        image_type: str = Form(None),
        file: UploadFile = File(...),
        location: str = Form(None),
        has_fault: Optional[bool] = Form(None),
        db: Session = Depends(get_db)
):
    """上传几何量数据（图片）- 直接调用接口"""
    return await upload_image_data(
        background_tasks=background_tasks,
        device_id=device_id,
        hardware_key=hardware_key,
        description=description,
        image_type=image_type,
        file=file,
        location=location,
        has_fault=has_fault,
        db=db
    )


# ==================== 数据查询接口 ====================

@app.get("/api/data/excel")
async def get_excel_data(
        device_id: Optional[str] = None,
        limit: int = 100,
        skip: int = 0,
        db: Session = Depends(get_db)
):
    """获取电量数据列表。"""
    try:
        query = db.query(MeterExcelData)
        if device_id:
            query = query.filter(MeterExcelData.device_id == device_id)
        total = query.count()
        files = query.order_by(desc(MeterExcelData.upload_time)).limit(limit).offset(skip).all()
        return {
            "status": "success",
            "total": total,
            "count": len(files),
            "data": [serialize_excel_record(db, file_record, include_parsed_data=False) for file_record in files],
        }
    except Exception as e:
        logger.error(f"Failed to get excel data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/data/image")
async def get_image_data(
        device_id: Optional[str] = None,
        limit: int = 100,
        skip: int = 0,
        db: Session = Depends(get_db)
):
    """获取几何量数据列表。"""
    try:
        query = db.query(MeterImageData)
        if device_id:
            query = query.filter(MeterImageData.device_id == device_id)
        total = query.count()
        files = query.order_by(desc(MeterImageData.upload_time)).limit(limit).offset(skip).all()
        return {
            "status": "success",
            "total": total,
            "count": len(files),
            "data": [serialize_image_record(db, file_record, include_analysis_data=False) for file_record in files],
        }
    except Exception as e:
        logger.error(f"Failed to get image data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/data/all")
async def get_all_data(
        device_id: Optional[str] = None,
        limit: int = 100,
        skip: int = 0,
        db: Session = Depends(get_db)
):
    """获取所有数据（电量和几何量）。"""
    try:
        excel_query = db.query(MeterExcelData)
        if device_id:
            excel_query = excel_query.filter(MeterExcelData.device_id == device_id)
        excel_files = excel_query.order_by(desc(MeterExcelData.upload_time)).limit(limit).offset(skip).all()

        image_query = db.query(MeterImageData)
        if device_id:
            image_query = image_query.filter(MeterImageData.device_id == device_id)
        image_files = image_query.order_by(desc(MeterImageData.upload_time)).limit(limit).offset(skip).all()

        return {
            "status": "success",
            "excel_data": {
                "count": len(excel_files),
                "data": [dict(serialize_excel_record(db, file_record, include_parsed_data=False), data_type="excel") for file_record in excel_files]
            },
            "image_data": {
                "count": len(image_files),
                "data": [dict(serialize_image_record(db, file_record, include_analysis_data=False), data_type="image") for file_record in image_files]
            }
        }
    except Exception as e:
        logger.error(f"Failed to get all data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/data/excel/{file_id}")
async def get_excel_detail(file_id: int, db: Session = Depends(get_db)):
    """获取电量数据详情。"""
    try:
        file_record = db.query(MeterExcelData).filter(MeterExcelData.id == file_id).first()
        if not file_record:
            raise HTTPException(status_code=404, detail="File not found")
        return {"status": "success", "data": serialize_excel_record(db, file_record, include_parsed_data=True)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get excel detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/data/image/{file_id}")
async def get_image_detail(file_id: int, db: Session = Depends(get_db)):
    """获取几何量数据详情。"""
    try:
        file_record = db.query(MeterImageData).filter(MeterImageData.id == file_id).first()
        if not file_record:
            raise HTTPException(status_code=404, detail="File not found")
        return {"status": "success", "data": serialize_image_record(db, file_record, include_analysis_data=True)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get image detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 文件下载接口 ====================

@app.get("/api/file/download/{data_type}/{file_id}")
async def download_file(data_type: str, file_id: int, db: Session = Depends(get_db)):
    """下载文件（通用接口）"""
    try:
        if data_type == "excel":
            file_record = db.query(MeterExcelData).filter(MeterExcelData.id == file_id).first()
            record_type = "电量数据"
        elif data_type == "image":
            file_record = db.query(MeterImageData).filter(MeterImageData.id == file_id).first()
            record_type = "几何量数据"
        else:
            raise HTTPException(status_code=400, detail="Invalid data type. Use 'excel' or 'image'")

        if not file_record:
            raise HTTPException(status_code=404, detail=f"{record_type}文件不存在")

        file_path = Path(file_record.file_path)

        if not file_path.exists():
            logger.error(f"文件不存在于磁盘: {file_path}")
            raise HTTPException(status_code=404, detail="File not found on disk")

        logger.info(f"下载{record_type}: {file_record.file_name}")

        return FileResponse(
            path=file_path,
            filename=file_record.file_name,
            media_type='application/octet-stream'
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"File download failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/file/download/excel/{file_id}")
async def download_excel_file(file_id: int, db: Session = Depends(get_db)):
    """下载电量数据文件"""
    return await download_file("excel", file_id, db)


@app.get("/api/file/download/image/{file_id}")
async def download_image_file(file_id: int, db: Session = Depends(get_db)):
    """下载几何量数据文件"""
    return await download_file("image", file_id, db)


# ==================== 通知接口 ====================


@app.get("/api/notifications")
async def list_notifications(
        notification_type: str = None,
        device_id: str = None,
        status: str = None,
        keyword: str = None,
        limit: int = 100,
        skip: int = 0,
        db: Session = Depends(get_db)
):
    """查询通知历史，支持按类型、设备、状态和关键词过滤。"""
    try:
        query = db.query(Notification)

        if notification_type:
            query = query.filter(Notification.notification_type == notification_type)
        if device_id:
            query = query.filter(Notification.device_id == device_id)
        if status:
            query = query.filter(Notification.status == status)
        if keyword:
            query = query.filter(Notification.message.like(f"%{keyword}%"))

        total = query.count()
        notifications = query.order_by(desc(Notification.created_at)).limit(limit).offset(skip).all()

        return {
            "status": "success",
            "total": total,
            "count": len(notifications),
            "notifications": [
                {
                    "id": n.id,
                    "device_id": n.device_id,
                    "notification_type": n.notification_type,
                    "message": n.message,
                    "status": n.status,
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                    "read_at": n.read_at.isoformat() if n.read_at else None,
                }
                for n in notifications
            ]
        }
    except Exception as e:
        logger.error(f"Failed to list notifications: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/notifications/unread")
async def get_unread_notifications(db: Session = Depends(get_db)):
    """获取未读通知"""
    try:
        notifications = db.query(Notification).filter(
            Notification.status == "unread"
        ).order_by(desc(Notification.created_at)).all()

        return {
            "status": "success",
            "count": len(notifications),
            "notifications": [
                {
                    "id": n.id,
                    "device_id": n.device_id,
                    "notification_type": n.notification_type,
                    "message": n.message,
                    "created_at": n.created_at.isoformat()
                }
                for n in notifications
            ]
        }
    except Exception as e:
        logger.error(f"Failed to get notifications: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: int, db: Session = Depends(get_db)):
    """标记通知为已读"""
    try:
        notification = db.query(Notification).filter(Notification.id == notification_id).first()
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")

        notification.status = "read"
        notification.read_at = datetime.now()
        db.commit()

        return {"status": "success", "message": "Notification marked as read"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to mark notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 统计接口 ====================

@app.get("/api/statistics/device/{device_id}")
async def get_device_statistics(device_id: str, db: Session = Depends(get_db)):
    """获取设备数据统计"""
    try:
        # 统计电量数据
        excel_count = db.query(func.count(MeterExcelData.id)).filter(
            MeterExcelData.device_id == device_id
        ).scalar() or 0

        excel_size = db.query(func.sum(MeterExcelData.file_size)).filter(
            MeterExcelData.device_id == device_id
        ).scalar() or 0

        # 统计几何量数据
        image_count = db.query(func.count(MeterImageData.id)).filter(
            MeterImageData.device_id == device_id
        ).scalar() or 0

        image_size = db.query(func.sum(MeterImageData.file_size)).filter(
            MeterImageData.device_id == device_id
        ).scalar() or 0

        original_size = db.query(func.sum(MeterImageData.original_size)).filter(
            MeterImageData.device_id == device_id
        ).scalar() or 0

        compression_saved = (original_size - image_size) if original_size and image_size else 0

        # 获取最近数据时间
        last_excel = db.query(MeterExcelData).filter(
            MeterExcelData.device_id == device_id
        ).order_by(desc(MeterExcelData.upload_time)).first()

        last_image = db.query(MeterImageData).filter(
            MeterImageData.device_id == device_id
        ).order_by(desc(MeterImageData.upload_time)).first()

        return {
            "status": "success",
            "device_id": device_id,
            "excel_data": {
                "count": excel_count,
                "total_size": excel_size,
                "last_upload": last_excel.upload_time.isoformat() if last_excel else None
            },
            "image_data": {
                "count": image_count,
                "total_size": image_size,
                "original_size": original_size,
                "compression_saved": compression_saved,
                "last_upload": last_image.upload_time.isoformat() if last_image else None
            },
            "summary": {
                "total_files": excel_count + image_count,
                "total_size": excel_size + image_size,
                "compression_saved": compression_saved
            }
        }
    except Exception as e:
        logger.error(f"Failed to get statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/statistics/overview")
async def get_system_overview(db: Session = Depends(get_db)):
    """获取系统统计概览"""
    try:
        # 统计设备数
        device_count = db.query(func.count(Device.id)).scalar() or 0
        online_count = db.query(func.count(Device.id)).filter(Device.status == "online").scalar() or 0

        # 统计数据
        total_excel = db.query(func.count(MeterExcelData.id)).scalar() or 0
        total_excel_size = db.query(func.sum(MeterExcelData.file_size)).scalar() or 0

        total_image = db.query(func.count(MeterImageData.id)).scalar() or 0
        total_image_size = db.query(func.sum(MeterImageData.file_size)).scalar() or 0
        total_original_size = db.query(func.sum(MeterImageData.original_size)).scalar() or 0

        compression_saved = (total_original_size - total_image_size) if total_original_size and total_image_size else 0

        return {
            "status": "success",
            "devices": {
                "total": device_count,
                "online": online_count,
                "offline": device_count - online_count
            },
            "data": {
                "excel_count": total_excel,
                "excel_size": total_excel_size,
                "image_count": total_image,
                "image_size": total_image_size,
                "image_original_size": total_original_size,
                "compression_saved": compression_saved,
                "total_files": total_excel + total_image,
                "total_size": total_excel_size + total_image_size
            }
        }
    except Exception as e:
        logger.error(f"Failed to get overview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 设备管理接口 ====================

@app.get("/api/devices/list")
async def list_devices(db: Session = Depends(get_db)):
    """获取设备列表"""
    try:
        devices = db.query(Device).all()

        return {
            "status": "success",
            "count": len(devices),
            "devices": [
                {
                    "device_id": d.device_id,
                    "device_name": d.device_name,
                    "device_ip": d.device_ip,
                    "location": d.location,
                    "status": d.status,

                    "created_at": d.created_at.isoformat(),
                    "updated_at": d.updated_at.isoformat()
                }
                for d in devices
            ]
        }
    except Exception as e:
        logger.error(f"Failed to list devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 健康检查 ====================

@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "三相表数据管理系统",
        "timestamp": datetime.now().isoformat()
    }


# ==================== 辅助函数 ====================

def update_statistics(db: Session, device_id: str, data_type: str,
                      file_size: int, original_size: int = None):
    """Update daily upload statistics and tolerate legacy NULL values."""
    try:
        today = datetime.now().date()
        stat = db.query(DataStatistics).filter(
            DataStatistics.device_id == device_id,
            DataStatistics.date >= datetime.combine(today, datetime.min.time()),
            DataStatistics.date < datetime.combine(today + timedelta(days=1), datetime.min.time())
        ).first()

        if not stat:
            stat = DataStatistics(
                device_id=device_id,
                date=datetime.now(),
                excel_count=0,
                excel_total_size=0,
                image_count=0,
                image_total_size=0,
                image_original_size=0,
            )
            db.add(stat)

        # Legacy rows can contain NULL values; normalize before arithmetic.
        stat.excel_count = stat.excel_count or 0
        stat.excel_total_size = stat.excel_total_size or 0
        stat.image_count = stat.image_count or 0
        stat.image_total_size = stat.image_total_size or 0
        stat.image_original_size = stat.image_original_size or 0

        safe_file_size = int(file_size or 0)
        safe_original_size = int(original_size or 0)

        if data_type == "excel":
            stat.excel_count += 1
            stat.excel_total_size += safe_file_size
        elif data_type == "image":
            stat.image_count += 1
            stat.image_total_size += safe_file_size
            if safe_original_size > 0:
                stat.image_original_size += safe_original_size

        stat.updated_at = datetime.now()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update statistics: {e}")

@app.post("/api/device/set-status")
async def set_device_status(
        device_id: str = Form(...),
        hardware_key: str = Form(...),
        status: str = Form(...),  # 'online' 或 'offline'
        db: Session = Depends(get_db)
):
    """设置设备状态"""
    try:
        # 验证设备
        device = db.query(Device).filter(Device.device_id == device_id).first()

        if not device:
            logger.warning(f"设备不存在: {device_id}")
            raise HTTPException(status_code=404, detail="Device not found")

        # 验证硬件密钥
        if not security_manager.verify_hardware_key(hardware_key, device.hardware_key):
            logger.warning(f"硬件密钥错误: {device_id}")
            raise HTTPException(status_code=401, detail="Invalid hardware key")

        # 验证状态值
        if status not in ['online', 'offline']:
            raise HTTPException(status_code=400, detail="Invalid status. Use 'online' or 'offline'")

        # 更新设备状态
        device.status = status
        device.updated_at = datetime.now()
        db.commit()

        logger.info(f"设备状态已更新: {device_id} -> {status}")

        return {
            "status": "success",
            "message": f"Device status updated to {status}",
            "device_id": device_id,
            "device_status": status
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set device status: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/device/offline")
async def set_device_offline(
        device_id: str = Form(...),
        hardware_key: str = Form(...),
        db: Session = Depends(get_db)
):
    """设置设备离线"""
    try:
        device = db.query(Device).filter(Device.device_id == device_id).first()

        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        if not security_manager.verify_hardware_key(hardware_key, device.hardware_key):
            raise HTTPException(status_code=401, detail="Invalid hardware key")

        device.status = "offline"
        device.updated_at = datetime.now()
        db.commit()

        logger.info(f"设备已离线: {device_id}")

        return {
            "status": "success",
            "message": "Device set to offline",
            "device_id": device_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set device offline: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=config.server_host,
        port=config.server_port,
        reload=True
    )
