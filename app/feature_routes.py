import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Body
from sqlalchemy import and_, desc, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import (
    get_db, Device, DeviceIdReservation, Hardware_Key, DeviceRegistrationRequest,
    Notification, FaultRecord, DataSearchIndex, AlarmRule,
    MeterExcelMeasurementDetail, MeterExcelErrorDetail
)
from app.security import security_manager
from app.feature_services import (
    ensure_search_index, remember_polling_notification,
    polling_client_offsets, polling_notifications, create_alarm_notification, ensure_default_alarm_rules
)

router = APIRouter()


def _notification_to_polling_payload(notification: Notification) -> dict:
    """把数据库未读预警通知转换成长轮询消息，实现上位机离线后的持久补发。"""
    message = notification.message or "收到新的报警预警"
    return {
        "type": notification.notification_type or "fault_alarm",
        "notification_id": notification.id,
        "device_id": notification.device_id,
        "file_id": notification.file_id,
        "file_name": notification.file_name,
        "data_type": notification.data_type,
        "fault_summary": message,
        "message": message,
        "timestamp": notification.created_at.isoformat() if notification.created_at else datetime.now().isoformat(),
        "from_offline_queue": True,
    }


@router.get("/api/polling/notifications")
async def polling_notifications_endpoint(
        client_id: str,
        timeout: int = 30,
        device_id: Optional[str] = None,
        db: Session = Depends(get_db)
):
    """Upper client long-polling endpoint for upload, approval, and fault events."""
    timeout = max(1, min(timeout, 30))
    deadline = datetime.now() + timedelta(seconds=timeout)

    while True:
        # 预警通知必须先查数据库未读队列：上位机离线时内存轮询收不到，数据库仍可补发。
        pending_query = db.query(Notification).filter(
            Notification.status == "unread",
            Notification.notification_type == "fault_alarm",
        )
        if device_id:
            pending_query = pending_query.filter(Notification.device_id == device_id)
        # 离线预警分小批补发，避免上位机启动时一次性收到大量 toast/状态栏刷新导致界面崩溃。
        pending_notifications = pending_query.order_by(Notification.id.asc()).limit(10).all()
        if pending_notifications:
            notifications = [_notification_to_polling_payload(item) for item in pending_notifications]
            return {"status": "success", "count": len(notifications), "notifications": notifications}

        last_id = polling_client_offsets.get(client_id, 0)
        notifications = [
            item for item in polling_notifications
            if item.get("polling_id", 0) > last_id
            and item.get("type") != "fault_alarm"
            and (not device_id or item.get("device_id") == device_id)
        ]
        if notifications:
            polling_client_offsets[client_id] = max(item["polling_id"] for item in notifications)
            return {"status": "success", "count": len(notifications), "notifications": notifications}

        if datetime.now() >= deadline:
            return {"status": "success", "count": 0, "notifications": []}
        await asyncio.sleep(1)


@router.post("/api/polling/heartbeat")
async def polling_heartbeat(
        device_id: str = Form(...),
        hardware_key: str = Form(...),
        location: str = Form(None),
        db: Session = Depends(get_db)
):
    """Lower client heartbeat endpoint used by the HTTP long-polling mode."""
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not security_manager.verify_hardware_key(hardware_key, device.hardware_key):
        raise HTTPException(status_code=401, detail="Invalid hardware key")
    device.status = "online"
    # 心跳携带城市时持续刷新设备在线位置，避免设备移动后上位机仍显示旧位置。
    if location:
        device.location = location
    device.updated_at = datetime.now()
    db.commit()
    return {"status": "success", "device_id": device_id, "device_status": "online"}


@router.get("/api/device/registration-requests")
async def list_registration_requests(
        status: Optional[str] = Query(None),
        device_id: Optional[str] = Query(None),
        keyword: Optional[str] = Query(None),
        db: Session = Depends(get_db)
):
    """List registration requests for the upper-client approval page."""
    query = db.query(DeviceRegistrationRequest)
    if status:
        query = query.filter(DeviceRegistrationRequest.status == status)
    if device_id:
        query = query.filter(DeviceRegistrationRequest.device_id == device_id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(
            DeviceRegistrationRequest.device_id.like(like),
            DeviceRegistrationRequest.device_name.like(like),
            DeviceRegistrationRequest.device_ip.like(like),
            DeviceRegistrationRequest.location.like(like),
            DeviceRegistrationRequest.hardware_key.like(like),
            DeviceRegistrationRequest.review_message.like(like)
        ))
    rows = query.order_by(desc(DeviceRegistrationRequest.requested_at), desc(DeviceRegistrationRequest.id)).all()
    return {
        "status": "success",
        "count": len(rows),
        "requests": [
            {
                "id": row.id,
                "device_id": row.device_id,
                "device_name": row.device_name,
                "device_ip": row.device_ip,
                "location": row.location,
                "hardware_key": row.hardware_key,
                "status": row.status,
                "review_message": row.review_message,
                "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            }
            for row in rows
        ]
    }


@router.get("/api/device/registration-status")
async def get_registration_status(
        device_id: str = Query(...),
        hardware_key: str = Query(...),
        db: Session = Depends(get_db)
):
    """Return the current approval progress for a lower-client device."""
    device_id_key = device_id.strip().casefold()
    device = db.query(Device).filter(func.lower(Device.device_id) == device_id_key).first()
    latest_request = db.query(DeviceRegistrationRequest).filter(
        func.lower(DeviceRegistrationRequest.device_id) == device_id_key,
        DeviceRegistrationRequest.hardware_key == hardware_key,
    ).order_by(desc(DeviceRegistrationRequest.requested_at), desc(DeviceRegistrationRequest.id)).first()

    if device and security_manager.verify_hardware_key(hardware_key, device.hardware_key):
        return {
            "status": "success",
            "registration_status": "approved",
            "device_exists": True,
            "request_id": latest_request.id if latest_request else None,
            "review_message": latest_request.review_message if latest_request else "审批通过",
            "requested_at": latest_request.requested_at.isoformat() if latest_request and latest_request.requested_at else None,
            "reviewed_at": latest_request.reviewed_at.isoformat() if latest_request and latest_request.reviewed_at else None,
            "queue_position": 0,
            "location": device.location,
            "status_text": "设备审批已通过，可连接服务器"
        }

    # 查询者硬件与正式设备或注册占用者不一致时，明确告知设备 ID 冲突。
    reservation = db.query(DeviceIdReservation).filter(
        DeviceIdReservation.device_id == device_id_key
    ).first()
    if device or (reservation and reservation.hardware_key != hardware_key):
        return {
            "status": "success",
            "registration_status": "conflict",
            "device_exists": bool(device),
            "request_id": None,
            "review_message": "该设备ID已绑定其他硬件",
            "requested_at": None,
            "reviewed_at": None,
            "queue_position": None,
            "location": device.location if device else None,
            "status_text": f"设备ID {device_id} 已被占用，请更换设备ID",
        }

    if latest_request:
        queue_position = None
        status_text = "注册状态未知"
        if latest_request.status == "pending":
            queue_position = db.query(DeviceRegistrationRequest).filter(
                DeviceRegistrationRequest.status == "pending"
            ).filter(or_(
                DeviceRegistrationRequest.requested_at < latest_request.requested_at,
                and_(
                    DeviceRegistrationRequest.requested_at == latest_request.requested_at,
                    DeviceRegistrationRequest.id <= latest_request.id
                )
            )).count()
            status_text = f"注册申请待审批，当前排队第 {queue_position} 位"
        elif latest_request.status == "rejected":
            status_text = "注册申请已驳回，请根据审批意见调整后重新提交"
        elif latest_request.status == "approved":
            status_text = "注册申请已批准，等待设备连接"

        return {
            "status": "success",
            "registration_status": latest_request.status,
            "device_exists": False,
            "request_id": latest_request.id,
            "review_message": latest_request.review_message,
            "requested_at": latest_request.requested_at.isoformat() if latest_request.requested_at else None,
            "reviewed_at": latest_request.reviewed_at.isoformat() if latest_request.reviewed_at else None,
            "queue_position": queue_position,
            "location": latest_request.location,
            "status_text": status_text
        }

    return {
        "status": "success",
        "registration_status": "unregistered",
        "device_exists": False,
        "request_id": None,
        "review_message": None,
        "requested_at": None,
        "reviewed_at": None,
        "queue_position": None,
        "location": None,
        "status_text": "当前设备还没有提交注册申请"
    }


@router.post("/api/device/registration-requests/{request_id}/approve")
async def approve_registration_request(
        request_id: int,
        review_message: str = Form("approved"),
        db: Session = Depends(get_db)
):
    """Approve one registration request while preserving device-ID uniqueness."""
    item = db.query(DeviceRegistrationRequest).filter(
        DeviceRegistrationRequest.id == request_id
    ).with_for_update().first()
    if not item:
        raise HTTPException(status_code=404, detail="注册申请不存在")
    if item.status != "pending":
        raise HTTPException(status_code=400, detail="注册申请已经审批")

    device_id_key = item.device_id.strip().casefold()

    # 审批阶段再次锁定设备 ID，防止两个历史重复申请被并发批准。
    reservation = db.query(DeviceIdReservation).filter(
        DeviceIdReservation.device_id == device_id_key
    ).with_for_update().first()
    if reservation and reservation.hardware_key != item.hardware_key:
        raise HTTPException(
            status_code=409,
            detail=f"设备ID {item.device_id} 已被其他设备占用，不能批准该申请",
        )
    if not reservation:
        reservation = DeviceIdReservation(
            device_id=device_id_key,
            hardware_key=item.hardware_key,
            request_id=item.id,
            status="pending",
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"设备ID {item.device_id} 已被其他申请占用",
            )

    hardware_key = db.query(Hardware_Key).filter(
        Hardware_Key.hardware_key == item.hardware_key
    ).first()
    if not hardware_key:
        db.add(Hardware_Key(hardware_key=item.hardware_key))

    device = db.query(Device).filter(
        func.lower(Device.device_id) == device_id_key
    ).with_for_update().first()
    if device and not security_manager.verify_hardware_key(item.hardware_key, device.hardware_key):
        raise HTTPException(
            status_code=409,
            detail=f"设备ID {item.device_id} 已绑定其他硬件",
        )
    if not device:
        db.add(Device(
            device_id=item.device_id.strip(),
            device_name=item.device_name,
            device_ip=item.device_ip,
            location=item.location,
            hardware_key=item.hardware_key,
            status="offline",
        ))
    else:
        device.device_name = item.device_name
        device.device_ip = item.device_ip
        device.location = item.location or device.location
        device.hardware_key = item.hardware_key
        device.status = "offline"
        device.updated_at = datetime.now()

    item.status = "approved"
    item.review_message = review_message
    item.reviewed_at = datetime.now()
    reservation.hardware_key = item.hardware_key
    reservation.request_id = item.id
    reservation.status = "approved"
    reservation.updated_at = datetime.now()

    # 升级前若已存在同 ID 的重复待审记录，批准一个后自动驳回其余记录。
    duplicate_requests = db.query(DeviceRegistrationRequest).filter(
        func.lower(DeviceRegistrationRequest.device_id) == device_id_key,
        DeviceRegistrationRequest.status == "pending",
        DeviceRegistrationRequest.id != item.id,
    ).all()
    for duplicate in duplicate_requests:
        duplicate.status = "rejected"
        duplicate.review_message = f"设备ID已由申请 {item.id} 占用"
        duplicate.reviewed_at = datetime.now()

    db.add(Notification(
        device_id=item.device_id,
        notification_type="device_register_approved",
        message=f"设备注册审批通过: {item.device_id} - {review_message}",
        status="unread",
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"设备ID {item.device_id} 已存在，审批未执行",
        )

    remember_polling_notification({
        "type": "device_register_approved",
        "device_id": item.device_id,
        "data_type": "设备管理",
        "request_id": item.id,
        "review_message": review_message,
        "location": item.location,
        "timestamp": datetime.now().isoformat(),
    })
    return {
        "status": "success",
        "message": "设备注册申请已批准",
        "device_id": item.device_id,
        "location": item.location,
    }


@router.post("/api/device/registration-requests/{request_id}/reject")
async def reject_registration_request(
        request_id: int,
        review_message: str = Form("rejected"),
        db: Session = Depends(get_db)
):
    """Reject a pending request and release its device-ID reservation."""
    item = db.query(DeviceRegistrationRequest).filter(
        DeviceRegistrationRequest.id == request_id
    ).with_for_update().first()
    if not item:
        raise HTTPException(status_code=404, detail="注册申请不存在")
    if item.status != "pending":
        raise HTTPException(status_code=400, detail="注册申请已经审批")

    item.status = "rejected"
    item.review_message = review_message
    item.reviewed_at = datetime.now()
    device_id_key = item.device_id.strip().casefold()

    # 驳回占用者后，优先把 ID 转交给旧库中下一条待审记录，否则彻底释放。
    reservation = db.query(DeviceIdReservation).filter(
        DeviceIdReservation.device_id == device_id_key
    ).with_for_update().first()
    if reservation and reservation.hardware_key == item.hardware_key:
        next_request = db.query(DeviceRegistrationRequest).filter(
            func.lower(DeviceRegistrationRequest.device_id) == device_id_key,
            DeviceRegistrationRequest.status == "pending",
            DeviceRegistrationRequest.id != item.id,
        ).order_by(DeviceRegistrationRequest.id.asc()).first()
        if next_request:
            reservation.hardware_key = next_request.hardware_key
            reservation.request_id = next_request.id
            reservation.status = "pending"
            reservation.updated_at = datetime.now()
        else:
            db.delete(reservation)

    db.add(Notification(
        device_id=item.device_id,
        notification_type="device_register_rejected",
        message=f"设备注册审批驳回: {item.device_id} - {review_message}",
        status="unread",
    ))
    db.commit()

    remember_polling_notification({
        "type": "device_register_rejected",
        "device_id": item.device_id,
        "data_type": "设备管理",
        "request_id": item.id,
        "review_message": review_message,
        "timestamp": datetime.now().isoformat(),
    })
    return {
        "status": "success",
        "message": "设备注册申请已驳回，设备ID占用已释放",
        "device_id": item.device_id,
    }


@router.get("/api/alarm-rules")
async def list_alarm_rules(
        data_type: Optional[str] = Query(None),
        enabled: Optional[bool] = Query(None),
        db: Session = Depends(get_db)
):
    """列出预警规则，供上位机预警配置页查看和编辑。"""
    ensure_default_alarm_rules(db)
    query = db.query(AlarmRule)
    if data_type:
        query = query.filter(AlarmRule.data_type == data_type)
    if enabled is not None:
        query = query.filter(AlarmRule.enabled == enabled)
    rows = query.order_by(AlarmRule.data_type.asc(), AlarmRule.id.asc()).all()
    return {
        "status": "success",
        "count": len(rows),
        "rules": [
            {
                "id": row.id,
                "rule_name": row.rule_name,
                "data_type": row.data_type,
                "metric_key": row.metric_key,
                "operator": row.operator,
                "threshold_value": row.threshold_value,
                "enabled": row.enabled,
                "severity": row.severity,
                "description": row.description,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]
    }


@router.post("/api/alarm-rules/save")
async def save_alarm_rule(
        payload: dict = Body(...),
        db: Session = Depends(get_db)
):
    """新增或更新预警规则。"""
    ensure_default_alarm_rules(db)
    rule_id = payload.get("id")
    if rule_id:
        rule = db.query(AlarmRule).filter(AlarmRule.id == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="Alarm rule not found")
    else:
        rule = AlarmRule()
        db.add(rule)

    rule.rule_name = payload.get("rule_name", rule.rule_name or "未命名规则")
    rule.data_type = payload.get("data_type", rule.data_type or "excel")
    rule.metric_key = payload.get("metric_key", rule.metric_key or "file_size_kb")
    rule.operator = payload.get("operator", rule.operator or "gt")
    rule.threshold_value = payload.get("threshold_value")
    rule.enabled = bool(payload.get("enabled", True))
    rule.severity = payload.get("severity", rule.severity or "warning")
    rule.description = payload.get("description", rule.description)
    rule.updated_at = datetime.now()
    if not getattr(rule, 'created_at', None):
        rule.created_at = datetime.now()

    db.commit()
    db.refresh(rule)
    return {
        "status": "success",
        "message": "Alarm rule saved",
        "rule": {
            "id": rule.id,
            "rule_name": rule.rule_name,
            "data_type": rule.data_type,
            "metric_key": rule.metric_key,
            "operator": rule.operator,
            "threshold_value": rule.threshold_value,
            "enabled": rule.enabled,
            "severity": rule.severity,
            "description": rule.description,
            "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
        }
    }
@router.post("/api/faults/report")
async def report_fault(
        device_id: str = Form(...),
        data_type: str = Form(...),
        file_id: int = Form(...),
        message: str = Form(...),
        severity: str = Form("warning"),
        db: Session = Depends(get_db)
):
    """Manual fault feedback endpoint used by upper client or test scripts."""
    if data_type not in {"excel", "image"}:
        raise HTTPException(status_code=400, detail="data_type must be excel or image")

    fault = FaultRecord(
        device_id=device_id,
        data_type=data_type,
        file_id=file_id,
        fault_type="人工反馈",
        severity=severity,
        message=message,
        source="manual",
        status="open"
    )
    db.add(fault)

    index = db.query(DataSearchIndex).filter(
        DataSearchIndex.data_type == data_type,
        DataSearchIndex.file_id == file_id
    ).first()
    if index:
        index.has_fault = True
        index.fault_summary = message
    db.commit()
    db.refresh(fault)

    create_alarm_notification(
        db=db,
        device_id=device_id,
        message=f"人工预警: {device_id} - {message}",
        notification_type="fault_alarm",
        data_type=data_type,
        file_id=file_id,
        file_name=index.file_name if index else None,
    )

    remember_polling_notification({
        "type": "fault_alarm",
        "device_id": device_id,
        "file_id": file_id,
        "file_name": index.file_name if index else None,
        "data_type": data_type,
        "fault_id": fault.id,
        "fault_summary": message,
        "timestamp": datetime.now().isoformat()
    })
    return {"status": "success", "fault_id": fault.id}


@router.get("/api/faults")
async def list_faults(
        device_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        skip: int = 0,
        db: Session = Depends(get_db)
):
    """Query fault records for alarm feedback views."""
    query = db.query(FaultRecord)
    if device_id:
        query = query.filter(FaultRecord.device_id == device_id)
    if status:
        query = query.filter(FaultRecord.status == status)
    total = query.count()
    rows = query.order_by(desc(FaultRecord.created_at)).limit(limit).offset(skip).all()
    # 一次批量读取文件索引，避免报警表每行再单独查询文件名。
    file_ids = {row.file_id for row in rows if row.file_id}
    file_indexes = db.query(DataSearchIndex).filter(
        DataSearchIndex.file_id.in_(file_ids)
    ).all() if file_ids else []
    file_name_map = {
        (item.data_type, item.file_id): item.file_name
        for item in file_indexes
    }
    return {
        "status": "success",
        "total": total,
        "count": len(rows),
        "faults": [
            {
                "id": row.id,
                "device_id": row.device_id,
                "data_type": row.data_type,
                "file_id": row.file_id,
                "file_name": file_name_map.get((row.data_type, row.file_id)),
                "download_url": f"/api/file/download/{row.data_type}/{row.file_id}",
                "fault_type": row.fault_type,
                "severity": row.severity,
                "message": row.message,
                "source": row.source,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }



@router.post("/api/faults/{fault_id}/status")
async def update_fault_status(
        fault_id: int,
        status: str = Form(...),
        db: Session = Depends(get_db)
):
    """Update fault alarm status from the upper-client alarm view."""
    if status not in {"open", "acknowledged", "closed"}:
        raise HTTPException(status_code=400, detail="status must be open, acknowledged or closed")
    fault = db.query(FaultRecord).filter(FaultRecord.id == fault_id).first()
    if not fault:
        raise HTTPException(status_code=404, detail="Fault record not found")
    fault.status = status
    db.commit()
    return {"status": "success", "fault_id": fault_id, "fault_status": status}

def _clean_query_text(value):
    """统一清理检索入参，空字符串按未填写处理。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sheet_candidates(sheet_name: Optional[str]):
    """兼容用户输入 A/B/C 和界面展示的 Sheet A/Sheet B。"""
    text = _clean_query_text(sheet_name)
    if not text:
        return []
    lower = text.lower()
    compact = text
    if lower.startswith("sheet "):
        compact = text[6:].strip()
    elif lower.startswith("sheet") and len(text) > 5:
        compact = text[5:].strip()
    candidates = {text, compact}
    if compact:
        candidates.add(f"Sheet {compact}")
    return [item for item in candidates if item]


def _has_excel_measurement_filters(filters: dict) -> bool:
    """判断是否需要进入电量 Excel 数值明细表检索。"""
    keys = (
        "excel_sheet_name", "excel_metric_key", "excel_phase_name",
        "excel_meter_name", "excel_range_text", "excel_value_min", "excel_value_max",
    )
    return any(filters.get(key) not in (None, "") for key in keys)


def _has_excel_error_filters(filters: dict) -> bool:
    """判断是否需要进入电量 Excel 误差明细表检索。"""
    keys = (
        "excel_sheet_name", "excel_metric_key", "excel_phase_name", "excel_range_text",
        "excel_error_percent_abs_min", "excel_error_percent_abs_max",
        "excel_error_ppm_abs_min", "excel_error_ppm_abs_max",
    )
    return any(filters.get(key) not in (None, "") for key in keys)


def _metric_conditions(model, metric_text: Optional[str]):
    """把界面输入的中文指标/英文 key 归一化，兼容旧数据和新明细表。"""
    text = _clean_query_text(metric_text)
    if not text:
        return []
    alias_map = {
        "power": ["power_w", "功率", "功率W", "功率w", "W"],
        "power_w": ["power_w", "功率", "功率W", "功率w", "W"],
        "功率": ["power_w", "功率", "功率W", "功率w", "W"],
        "功率w": ["power_w", "功率", "功率W", "功率w", "W"],
        "电压": ["voltage", "电压", "V"],
        "voltage": ["voltage", "电压", "V"],
        "电流": ["current", "电流", "A"],
        "current": ["current", "电流", "A"],
        "相角": ["phase_angle", "相角", "角度", "°"],
        "phase_angle": ["phase_angle", "相角", "角度", "°"],
        "angle": ["phase_angle", "相角", "角度", "°"],
    }
    aliases = alias_map.get(text.lower(), alias_map.get(text, [text]))
    clauses = []
    for item in aliases:
        clauses.append(model.metric_key == item)
        clauses.append(model.metric_name.like(f"%{item}%"))
    return [or_(*clauses)]


def _phase_candidates(value: Optional[str]):
    """兼容 A/A相/Sheet A 这类手输相位写法。"""
    text = _clean_query_text(value)
    if not text:
        return []
    compact = text.replace("相", "").replace("phase", "").replace("Phase", "").strip()
    candidates = {text}
    if compact:
        candidates.add(compact)
        candidates.add(f"{compact.upper()}相")
        candidates.add(f"{compact.lower()}相")
    return [item for item in candidates if item]


def _measurement_conditions(filters: dict, excel_id_expr=None):
    """构造 Excel 数值明细表条件，供接口查询和命中摘要复用。"""
    conditions = []
    if excel_id_expr is not None:
        conditions.append(MeterExcelMeasurementDetail.excel_id == excel_id_expr)
    sheet_candidates = _sheet_candidates(filters.get("excel_sheet_name"))
    if sheet_candidates:
        conditions.append(MeterExcelMeasurementDetail.sheet_name.in_(sheet_candidates))
    conditions.extend(_metric_conditions(MeterExcelMeasurementDetail, filters.get("excel_metric_key")))
    phase_candidates = _phase_candidates(filters.get("excel_phase_name"))
    if phase_candidates:
        conditions.append(MeterExcelMeasurementDetail.phase_name.in_(phase_candidates))
    meter_name = _clean_query_text(filters.get("excel_meter_name"))
    if meter_name:
        conditions.append(MeterExcelMeasurementDetail.meter_name.like(f"%{meter_name}%"))
    range_text = _clean_query_text(filters.get("excel_range_text"))
    if range_text:
        conditions.append(MeterExcelMeasurementDetail.range_text.like(f"%{range_text}%"))
    if filters.get("excel_value_min") is not None:
        conditions.append(MeterExcelMeasurementDetail.value >= filters["excel_value_min"])
    if filters.get("excel_value_max") is not None:
        conditions.append(MeterExcelMeasurementDetail.value <= filters["excel_value_max"])
    return conditions


def _error_conditions(filters: dict, excel_id_expr=None):
    """构造 Excel 误差明细表条件，支持按误差绝对值检索。"""
    conditions = []
    if excel_id_expr is not None:
        conditions.append(MeterExcelErrorDetail.excel_id == excel_id_expr)
    sheet_candidates = _sheet_candidates(filters.get("excel_sheet_name"))
    if sheet_candidates:
        conditions.append(MeterExcelErrorDetail.sheet_name.in_(sheet_candidates))
    conditions.extend(_metric_conditions(MeterExcelErrorDetail, filters.get("excel_metric_key")))
    phase_candidates = _phase_candidates(filters.get("excel_phase_name"))
    if phase_candidates:
        conditions.append(MeterExcelErrorDetail.phase_name.in_(phase_candidates))
    range_text = _clean_query_text(filters.get("excel_range_text"))
    if range_text:
        conditions.append(MeterExcelErrorDetail.range_text.like(f"%{range_text}%"))
    if filters.get("excel_error_percent_abs_min") is not None:
        conditions.append(func.abs(MeterExcelErrorDetail.error_percent) >= filters["excel_error_percent_abs_min"])
    if filters.get("excel_error_percent_abs_max") is not None:
        conditions.append(func.abs(MeterExcelErrorDetail.error_percent) <= filters["excel_error_percent_abs_max"])
    if filters.get("excel_error_ppm_abs_min") is not None:
        conditions.append(func.abs(MeterExcelErrorDetail.error_ppm) >= filters["excel_error_ppm_abs_min"])
    if filters.get("excel_error_ppm_abs_max") is not None:
        conditions.append(func.abs(MeterExcelErrorDetail.error_ppm) <= filters["excel_error_ppm_abs_max"])
    return conditions


def _excel_detail_keyword_filter(db: Session, like: str):
    """让关键词也能命中 Excel 明细；用子查询约束 file_id，避免 exists 在部分环境下失效。"""
    detail_excel_ids = db.query(MeterExcelMeasurementDetail.excel_id).filter(or_(
        MeterExcelMeasurementDetail.sheet_name.like(like),
        MeterExcelMeasurementDetail.metric_name.like(like),
        MeterExcelMeasurementDetail.metric_key.like(like),
        MeterExcelMeasurementDetail.phase_name.like(like),
        MeterExcelMeasurementDetail.meter_name.like(like),
        MeterExcelMeasurementDetail.range_text.like(like),
    )).distinct()
    return and_(DataSearchIndex.data_type == "excel", DataSearchIndex.file_id.in_(detail_excel_ids))


def _format_measurement_match(row: Optional[MeterExcelMeasurementDetail]):
    """把命中的电量数值明细转成上位机用户能直接看懂的摘要。"""
    if not row:
        return None, None
    unit = row.value_unit or ""
    summary = (
        f"{row.sheet_name} / {row.metric_name} / {row.phase_name} / {row.meter_name}"
        f" / {row.range_text or '测试点'} / 值={row.value}{unit}"
    )
    return summary, {
        "sheet_name": row.sheet_name,
        "metric_name": row.metric_name,
        "metric_key": row.metric_key,
        "phase_name": row.phase_name,
        "meter_name": row.meter_name,
        "range_text": row.range_text,
        "value": row.value,
        "value_unit": row.value_unit,
        "x_angle_degree": row.x_angle_degree,
        "x_current_a": row.x_current_a,
    }


def _format_error_match(row: Optional[MeterExcelErrorDetail]):
    """把命中的电量误差明细转成上位机用户能直接看懂的摘要。"""
    if not row:
        return None, None
    summary = (
        f"{row.sheet_name} / {row.metric_name} / {row.phase_name} / {row.range_text or '测试点'}"
        f" / {row.reference_meter_name}->{row.compared_meter_name}"
        f" / 误差={row.error_percent}% / {row.error_ppm}ppm"
    )
    return summary, {
        "sheet_name": row.sheet_name,
        "metric_name": row.metric_name,
        "metric_key": row.metric_key,
        "phase_name": row.phase_name,
        "range_text": row.range_text,
        "reference_meter_name": row.reference_meter_name,
        "compared_meter_name": row.compared_meter_name,
        "error_percent": row.error_percent,
        "error_ppm": row.error_ppm,
    }


def _excel_detail_result(db: Session, row: DataSearchIndex, filters: dict):
    """为检索结果补充第一条命中的 Excel 数值/误差明细。"""
    if row.data_type != "excel":
        return "", None, None
    measurement = None
    error = None
    if _has_excel_measurement_filters(filters):
        measurement = db.query(MeterExcelMeasurementDetail).filter(
            *_measurement_conditions(filters, row.file_id)
        ).order_by(MeterExcelMeasurementDetail.point_index.asc()).first()
    if _has_excel_error_filters(filters):
        error = db.query(MeterExcelErrorDetail).filter(
            *_error_conditions(filters, row.file_id)
        ).order_by(MeterExcelErrorDetail.point_index.asc()).first()
    measurement_summary, measurement_payload = _format_measurement_match(measurement)
    error_summary, error_payload = _format_error_match(error)
    return measurement_summary or error_summary or "", measurement_payload, error_payload


@router.get("/api/search/data")
async def search_data(
        data_type: Optional[str] = None,
        device_id: Optional[str] = None,
        device_prefix: Optional[str] = None,
        location: Optional[str] = None,
        has_fault: Optional[bool] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        keyword: Optional[str] = None,
        excel_sheet_name: Optional[str] = None,
        excel_metric_key: Optional[str] = None,
        excel_phase_name: Optional[str] = None,
        excel_meter_name: Optional[str] = None,
        excel_range_text: Optional[str] = None,
        excel_value_min: Optional[float] = None,
        excel_value_max: Optional[float] = None,
        excel_error_percent_abs_min: Optional[float] = None,
        excel_error_percent_abs_max: Optional[float] = None,
        excel_error_ppm_abs_min: Optional[float] = None,
        excel_error_ppm_abs_max: Optional[float] = None,
        limit: int = 100,
        skip: int = 0,
        db: Session = Depends(get_db)
):
    """Search dataset rows by file fields and fine-grained Excel measurement fields."""
    ensure_search_index(db)
    limit = max(1, min(limit, 1000))
    skip = max(0, skip)
    excel_filters = {
        "excel_sheet_name": _clean_query_text(excel_sheet_name),
        "excel_metric_key": _clean_query_text(excel_metric_key),
        "excel_phase_name": _clean_query_text(excel_phase_name),
        "excel_meter_name": _clean_query_text(excel_meter_name),
        "excel_range_text": _clean_query_text(excel_range_text),
        "excel_value_min": excel_value_min,
        "excel_value_max": excel_value_max,
        "excel_error_percent_abs_min": excel_error_percent_abs_min,
        "excel_error_percent_abs_max": excel_error_percent_abs_max,
        "excel_error_ppm_abs_min": excel_error_ppm_abs_min,
        "excel_error_ppm_abs_max": excel_error_ppm_abs_max,
    }

    query = db.query(DataSearchIndex)

    if data_type:
        query = query.filter(DataSearchIndex.data_type == data_type)
    if device_id:
        query = query.filter(DataSearchIndex.device_id == device_id)
    if device_prefix:
        query = query.filter(DataSearchIndex.device_id.like(f"{device_prefix}%"))
    if location:
        query = query.filter(DataSearchIndex.location == location)
    if has_fault is not None:
        query = query.filter(DataSearchIndex.has_fault == has_fault)
    if start_time:
        query = query.filter(DataSearchIndex.occurred_at >= start_time)
    if end_time:
        query = query.filter(DataSearchIndex.occurred_at <= end_time)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(
            DataSearchIndex.file_name.like(like),
            DataSearchIndex.device_id.like(like),
            DataSearchIndex.fault_summary.like(like),
            _excel_detail_keyword_filter(db, like),
        ))

    if _has_excel_measurement_filters(excel_filters):
        # 传入 Excel 数值细字段时，用明细表 excel_id 子查询直接约束主表 file_id。
        # 这样比相关 exists 更直观，也避免不同数据库方言下出现“条件未收窄”的情况。
        measurement_excel_ids = db.query(MeterExcelMeasurementDetail.excel_id).filter(
            *_measurement_conditions(excel_filters)
        ).distinct()
        query = query.filter(DataSearchIndex.data_type == "excel", DataSearchIndex.file_id.in_(measurement_excel_ids))

    error_only_keys = {
        "excel_error_percent_abs_min", "excel_error_percent_abs_max",
        "excel_error_ppm_abs_min", "excel_error_ppm_abs_max",
    }
    has_error_only_filter = any(excel_filters.get(key) is not None for key in error_only_keys)
    if has_error_only_filter:
        # 误差检索走误差明细表，并通过 excel_id 子查询回到数据集主表。
        error_excel_ids = db.query(MeterExcelErrorDetail.excel_id).filter(
            *_error_conditions(excel_filters)
        ).distinct()
        query = query.filter(DataSearchIndex.data_type == "excel", DataSearchIndex.file_id.in_(error_excel_ids))

    total = query.count()
    rows = query.order_by(desc(DataSearchIndex.occurred_at)).limit(limit).offset(skip).all()
    dataset = []
    for row in rows:
        detail_summary, detail_match, error_match = _excel_detail_result(db, row, excel_filters)
        dataset.append({
            "id": row.id,
            "data_type": row.data_type,
            "file_id": row.file_id,
            "device_id": row.device_id,
            "file_name": row.file_name,
            "location": row.location,
            "has_fault": row.has_fault,
            "fault_summary": row.fault_summary,
            "excel_detail_summary": detail_summary,
            "excel_detail_match": detail_match,
            "excel_error_match": error_match,
            "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
            "download_url": f"/api/file/download/{row.data_type}/{row.file_id}",
        })
    return {
        "status": "success",
        "total": total,
        "count": len(rows),
        "dataset": dataset,
    }


