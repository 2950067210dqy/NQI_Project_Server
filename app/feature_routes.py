import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Body
from sqlalchemy import and_, desc, or_
from sqlalchemy.orm import Session

from app.database import (
    get_db, Device, Hardware_Key, DeviceRegistrationRequest,
    Notification, FaultRecord, DataSearchIndex, AlarmRule
)
from app.security import security_manager
from app.feature_services import (
    ensure_search_index, remember_polling_notification,
    polling_client_offsets, polling_notifications, create_alarm_notification, ensure_default_alarm_rules
)

router = APIRouter()


@router.get("/api/polling/notifications")
async def polling_notifications_endpoint(
        client_id: str,
        timeout: int = 30,
        device_id: Optional[str] = None
):
    """Upper client long-polling endpoint for upload, approval, and fault events."""
    timeout = max(1, min(timeout, 30))
    deadline = datetime.now() + timedelta(seconds=timeout)

    while True:
        last_id = polling_client_offsets.get(client_id, 0)
        notifications = [
            item for item in polling_notifications
            if item.get("polling_id", 0) > last_id
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
    device = db.query(Device).filter(Device.device_id == device_id).first()
    latest_request = db.query(DeviceRegistrationRequest).filter(
        DeviceRegistrationRequest.device_id == device_id,
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
    """Approve a pending registration request and create/update the device record."""
    item = db.query(DeviceRegistrationRequest).filter(DeviceRegistrationRequest.id == request_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Registration request not found")
    if item.status != "pending":
        raise HTTPException(status_code=400, detail="Registration request already reviewed")

    hardware_key = db.query(Hardware_Key).filter(Hardware_Key.hardware_key == item.hardware_key).first()
    if not hardware_key:
        db.add(Hardware_Key(hardware_key=item.hardware_key))

    device = db.query(Device).filter(Device.device_id == item.device_id).first()
    if device and not security_manager.verify_hardware_key(item.hardware_key, device.hardware_key):
        raise HTTPException(status_code=400, detail="Device ID already bound to another hardware key")
    if not device:
        db.add(Device(
            device_id=item.device_id,
            device_name=item.device_name,
            device_ip=item.device_ip,
            location=item.location,
            hardware_key=item.hardware_key,
            status="offline"
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

    db.add(Notification(
        device_id=item.device_id,
        notification_type="device_register_approved",
        message=f"设备注册审批通过: {item.device_id} - {review_message}",
        status="unread"
    ))
    db.commit()

    remember_polling_notification({
        "type": "device_register_approved",
        "device_id": item.device_id,
        "data_type": "设备管理",
        "request_id": item.id,
        "review_message": review_message,
        "location": item.location,
        "timestamp": datetime.now().isoformat()
    })
    return {"status": "success", "message": "Registration request approved", "device_id": item.device_id, "location": item.location}


@router.post("/api/device/registration-requests/{request_id}/reject")
async def reject_registration_request(
        request_id: int,
        review_message: str = Form("rejected"),
        db: Session = Depends(get_db)
):
    """Reject a pending registration request and keep the review reason."""
    item = db.query(DeviceRegistrationRequest).filter(DeviceRegistrationRequest.id == request_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Registration request not found")
    if item.status != "pending":
        raise HTTPException(status_code=400, detail="Registration request already reviewed")

    item.status = "rejected"
    item.review_message = review_message
    item.reviewed_at = datetime.now()

    db.add(Notification(
        device_id=item.device_id,
        notification_type="device_register_rejected",
        message=f"设备注册审批驳回: {item.device_id} - {review_message}",
        status="unread"
    ))
    db.commit()

    remember_polling_notification({
        "type": "device_register_rejected",
        "device_id": item.device_id,
        "data_type": "设备管理",
        "request_id": item.id,
        "review_message": review_message,
        "timestamp": datetime.now().isoformat()
    })
    return {"status": "success", "message": "Registration request rejected", "device_id": item.device_id}


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
        notification_type="fault_alarm"
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
        limit: int = 100,
        skip: int = 0,
        db: Session = Depends(get_db)
):
    """Search dataset rows by time, location, fault flag, device, and keyword."""
    ensure_search_index(db)
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
            DataSearchIndex.fault_summary.like(like)
        ))

    total = query.count()
    rows = query.order_by(desc(DataSearchIndex.occurred_at)).limit(limit).offset(skip).all()
    return {
        "status": "success",
        "total": total,
        "count": len(rows),
        "dataset": [
            {
                "id": row.id,
                "data_type": row.data_type,
                "file_id": row.file_id,
                "device_id": row.device_id,
                "file_name": row.file_name,
                "location": row.location,
                "has_fault": row.has_fault,
                "fault_summary": row.fault_summary,
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
                "download_url": f"/api/file/download/{row.data_type}/{row.file_id}",
            }
            for row in rows
        ]
    }



