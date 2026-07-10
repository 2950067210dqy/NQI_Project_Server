from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pandas as pd
from sqlalchemy.orm import Session

from app.database import (
    AlarmRule,
    DataSearchIndex,
    FaultRecord,
    MeterExcelData,
    MeterImageData,
    Notification,
)

SEARCH_LOCATIONS = ["北京", "上海", "长沙", "苏州", "深圳"]
FAULT_KEYWORDS = ["故障", "异常", "报警", "fault", "error", "alarm", "warning"]

# HTTP polling notifications are kept in memory for upper clients using long polling.
polling_notifications = []
polling_client_offsets = {}
polling_notification_seq = 0

DEFAULT_ALARM_RULES = [
    {
        "rule_name": "电量数据文件大小过大",
        "data_type": "excel",
        "metric_key": "file_size_kb",
        "operator": "gt",
        "threshold_value": 2048,
        "enabled": True,
        "severity": "warning",
        "description": "电量数据文件大小超过阈值时触发预警"
    },
    {
        "rule_name": "电量数据最大数值超限",
        "data_type": "excel",
        "metric_key": "max_numeric_value",
        "operator": "gt",
        "threshold_value": 1000,
        "enabled": True,
        "severity": "warning",
        "description": "电量数据解析出的最大数值超过阈值时触发预警"
    },
    {
        "rule_name": "电量数据解析失败",
        "data_type": "excel",
        "metric_key": "processing_failed",
        "operator": "enabled",
        "threshold_value": 1,
        "enabled": True,
        "severity": "critical",
        "description": "服务端解析电量数据失败时触发预警"
    },
    {
        "rule_name": "几何量压缩率过高",
        "data_type": "image",
        "metric_key": "compression_ratio",
        "operator": "gt",
        "threshold_value": 90,
        "enabled": True,
        "severity": "warning",
        "description": "几何量图片压缩率过高时触发预警"
    },
    {
        "rule_name": "几何量文件大小过大",
        "data_type": "image",
        "metric_key": "file_size_kb",
        "operator": "gt",
        "threshold_value": 5120,
        "enabled": True,
        "severity": "warning",
        "description": "几何量图片大小超过阈值时触发预警"
    },
    {
        "rule_name": "几何量亮度异常",
        "data_type": "image",
        "metric_key": "mean_brightness",
        "operator": "lt",
        "threshold_value": 35,
        "enabled": True,
        "severity": "warning",
        "description": "图片过暗时触发预警"
    },
    {
        "rule_name": "几何量清晰度异常",
        "data_type": "image",
        "metric_key": "sharpness_score",
        "operator": "lt",
        "threshold_value": 12,
        "enabled": True,
        "severity": "warning",
        "description": "图片清晰度过低时触发预警"
    },
    {
        "rule_name": "几何量分析失败",
        "data_type": "image",
        "metric_key": "processing_failed",
        "operator": "enabled",
        "threshold_value": 1,
        "enabled": True,
        "severity": "critical",
        "description": "服务端分析几何量图片失败时触发预警"
    },
    {
        "rule_name": "电量数据上传错误告警",
        "data_type": "excel",
        "metric_key": "upload_error",
        "operator": "enabled",
        "threshold_value": 1,
        "enabled": True,
        "severity": "critical",
        "description": "电量数据上传失败时生成告警"
    },
    {
        "rule_name": "几何量数据上传错误告警",
        "data_type": "image",
        "metric_key": "upload_error",
        "operator": "enabled",
        "threshold_value": 1,
        "enabled": True,
        "severity": "critical",
        "description": "几何量数据上传失败时生成告警"
    },
]

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def infer_location(device_id: str) -> str:
    """Map a device id to a stable default location when upload metadata is absent."""
    text = device_id or "unknown"
    index = sum(ord(ch) for ch in text) % len(SEARCH_LOCATIONS)
    return SEARCH_LOCATIONS[index]


def normalize_bool(value, default=False) -> bool:
    """Normalize bool-like Form/Query values from clients and scripts."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "有", "故障"}


def detect_fault_flag(file_name: str, description: str = None, explicit_fault=None) -> tuple[bool, str]:
    """Detect a fault flag from explicit upload metadata or filename/description keywords."""
    if explicit_fault is not None:
        has_fault = normalize_bool(explicit_fault)
        return has_fault, "上传端显式标记故障" if has_fault else "上传端标记正常"

    haystack = ((file_name or "") + " " + (description or "")).lower()
    has_fault = any(keyword.lower() in haystack for keyword in FAULT_KEYWORDS)
    return has_fault, "文件名或描述命中故障关键字" if has_fault else "未发现故障特征"


def remember_polling_notification(payload: dict):
    """Store a notification for HTTP long-polling clients."""
    global polling_notification_seq
    polling_notification_seq += 1
    item = dict(payload)
    item["polling_id"] = polling_notification_seq
    polling_notifications.append(item)
    if len(polling_notifications) > 1000:
        del polling_notifications[: len(polling_notifications) - 1000]


def ensure_default_alarm_rules(db: Session):
    """初始化默认预警规则，保证上位机首次打开配置页时有可编辑规则。"""
    existing_keys = {
        (row.data_type, row.metric_key, row.rule_name)
        for row in db.query(AlarmRule.data_type, AlarmRule.metric_key, AlarmRule.rule_name).all()
    }
    created = False
    for item in DEFAULT_ALARM_RULES:
        key = (item["data_type"], item["metric_key"], item["rule_name"])
        if key in existing_keys:
            continue
        db.add(AlarmRule(**item))
        created = True
    if created:
        db.commit()


def compare_value(value, operator: str, threshold) -> bool:
    """按规则运算符比较数值。"""
    if operator == "gt":
        return value > threshold
    if operator == "ge":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "le":
        return value <= threshold
    if operator == "eq":
        return value == threshold
    if operator == "ne":
        return value != threshold
    if operator == "enabled":
        return True
    return False


def merge_fault_summary(base_summary: str, rule_messages: list[str]) -> str:
    """把基础故障描述和规则命中信息合并成统一预警摘要。"""
    messages = []
    if base_summary and base_summary not in {"未发现故障特征", "上传端标记正常", "图像分析正常"}:
        messages.append(base_summary)
    for message in rule_messages:
        if message and message not in messages:
            messages.append(message)
    return "；".join(messages) if messages else (base_summary or "检测到疑似故障")


def pick_higher_severity(left: str, right: str) -> str:
    """返回两个级别中更高的那个。"""
    if SEVERITY_ORDER.get(right, 1) > SEVERITY_ORDER.get(left, 1):
        return right
    return left


def extract_excel_metrics(file_bytes: bytes) -> dict:
    """提取电量数据中的基础统计量，供阈值告警规则使用。"""
    metrics = {
        "sheet_count": 0,
        "max_numeric_value": 0.0,
        "min_numeric_value": 0.0,
        "avg_numeric_value": 0.0,
        "numeric_value_count": 0,
    }
    try:
        excel_file = pd.ExcelFile(BytesIO(file_bytes))
        numeric_values = []
        metrics["sheet_count"] = len(excel_file.sheet_names)
        for sheet_name in excel_file.sheet_names:
            df = excel_file.parse(sheet_name, header=None)
            numeric_df = df.apply(pd.to_numeric, errors="coerce")
            values = numeric_df.to_numpy().flatten()
            numeric_values.extend(float(value) for value in values if pd.notna(value))
        if numeric_values:
            metrics["max_numeric_value"] = max(numeric_values)
            metrics["min_numeric_value"] = min(numeric_values)
            metrics["avg_numeric_value"] = sum(numeric_values) / len(numeric_values)
            metrics["numeric_value_count"] = len(numeric_values)
    except Exception:
        pass
    return metrics


def build_image_metrics(file_size: int, original_size: int, compression_ratio: float) -> dict:
    """整理几何量图片上传后的关键指标。"""
    return {
        "file_size_kb": round(file_size / 1024, 2),
        "original_size_kb": round((original_size or 0) / 1024, 2),
        "compression_ratio": round(compression_ratio or 0, 2),
    }


def evaluate_alarm_rules(db: Session, data_type: str, metrics: dict) -> tuple[list[str], str]:
    """根据预警规则判断当前上传或处理结果是否需要告警。"""
    ensure_default_alarm_rules(db)
    query = db.query(AlarmRule).filter(
        AlarmRule.enabled == True,
        AlarmRule.data_type.in_([data_type, "common"])
    )
    messages = []
    severity = "warning"
    for rule in query.all():
        if rule.metric_key == "upload_error":
            continue
        metric_value = metrics.get(rule.metric_key)
        if metric_value is None:
            continue
        if rule.operator != "enabled" and rule.threshold_value is None:
            continue
        if compare_value(metric_value, rule.operator, rule.threshold_value):
            if rule.operator == "enabled":
                messages.append(f"{rule.rule_name}: {metric_value}")
            else:
                messages.append(
                    f"{rule.rule_name}: {rule.metric_key}={metric_value} {rule.operator} {rule.threshold_value}"
                )
            severity = pick_higher_severity(severity, rule.severity or "warning")
    return messages, severity


def is_upload_error_alarm_enabled(db: Session, data_type: str) -> bool:
    """判断指定数据类型是否启用了上传错误告警。"""
    ensure_default_alarm_rules(db)
    return db.query(AlarmRule).filter(
        AlarmRule.enabled == True,
        AlarmRule.data_type.in_([data_type, "common"]),
        AlarmRule.metric_key == "upload_error"
    ).first() is not None


def ensure_search_index(db: Session):
    """Backfill search index rows for historical uploads before search runs."""
    existing = {
        (row.data_type, row.file_id)
        for row in db.query(DataSearchIndex.data_type, DataSearchIndex.file_id).all()
    }

    for record in db.query(MeterExcelData).all():
        key = ("excel", record.id)
        if key not in existing:
            has_fault, summary = detect_fault_flag(record.file_name, record.description)
            db.add(DataSearchIndex(
                data_type="excel",
                file_id=record.id,
                device_id=record.device_id,
                file_name=record.file_name,
                location=record.location or infer_location(record.device_id),
                has_fault=has_fault,
                fault_summary=summary,
                occurred_at=record.upload_time,
                uploaded_at=record.upload_time,
            ))

    for record in db.query(MeterImageData).all():
        key = ("image", record.id)
        if key not in existing:
            has_fault, summary = detect_fault_flag(record.file_name, record.description)
            db.add(DataSearchIndex(
                data_type="image",
                file_id=record.id,
                device_id=record.device_id,
                file_name=record.file_name,
                location=record.location or infer_location(record.device_id),
                has_fault=has_fault,
                fault_summary=summary,
                occurred_at=record.upload_time,
                uploaded_at=record.upload_time,
            ))
    db.commit()


def create_search_index(db: Session, data_type: str, record, location: str = None, has_fault=None, fault_summary: str = None):
    """Create the searchable dataset row immediately after an upload succeeds."""
    index = DataSearchIndex(
        data_type=data_type,
        file_id=record.id,
        device_id=record.device_id,
        file_name=record.file_name,
        location=location or infer_location(record.device_id),
        has_fault=normalize_bool(has_fault),
        fault_summary=fault_summary,
        occurred_at=getattr(record, "upload_time", datetime.now()),
        uploaded_at=getattr(record, "upload_time", datetime.now()),
    )
    db.add(index)
    db.commit()
    return index


def upsert_search_index(
        db: Session,
        data_type: str,
        record,
        location: str = None,
        has_fault: bool = False,
        fault_summary: str = None,
):
    """更新或创建搜索索引，让检索结果总是指向最新处理结论。"""
    index = db.query(DataSearchIndex).filter(
        DataSearchIndex.data_type == data_type,
        DataSearchIndex.file_id == record.id,
    ).first()
    if not index:
        return create_search_index(db, data_type, record, location, has_fault, fault_summary)

    index.device_id = record.device_id
    index.file_name = record.file_name
    index.location = location or getattr(record, "location", None) or infer_location(record.device_id)
    index.has_fault = normalize_bool(has_fault)
    index.fault_summary = fault_summary
    index.occurred_at = getattr(record, "processed_at", None) or getattr(record, "upload_time", datetime.now())
    index.uploaded_at = getattr(record, "upload_time", datetime.now())
    db.commit()
    db.refresh(index)
    return index


def create_fault_if_needed(
        db: Session,
        data_type: str,
        record,
        has_fault: bool,
        fault_summary: str,
        source: str = "auto",
        severity: str = "warning"
):
    """Persist an alarm record only when the upload is judged faulty."""
    if not has_fault:
        return None
    fault = FaultRecord(
        device_id=record.device_id,
        data_type=data_type,
        file_id=record.id,
        fault_type="数据异常",
        severity=severity,
        message=fault_summary or "检测到疑似故障",
        source=source,
        status="open",
    )
    db.add(fault)
    db.commit()
    db.refresh(fault)
    return fault


def sync_fault_record(
        db: Session,
        data_type: str,
        record,
        has_fault: bool,
        fault_summary: str,
        severity: str = "warning",
        source: str = "auto",
):
    """按文件维度维护唯一的自动告警记录，避免重复写多条。"""
    fault = db.query(FaultRecord).filter(
        FaultRecord.data_type == data_type,
        FaultRecord.file_id == record.id,
        FaultRecord.source == source,
    ).first()

    if not has_fault:
        if fault and fault.status != "closed":
            fault.status = "closed"
            fault.resolved_at = datetime.now()
            db.commit()
        return fault

    if not fault:
        return create_fault_if_needed(db, data_type, record, has_fault, fault_summary, source=source, severity=severity)

    fault.device_id = record.device_id
    fault.fault_type = "数据异常"
    fault.severity = severity
    fault.message = fault_summary or "检测到疑似故障"
    fault.status = "open"
    fault.resolved_at = None
    db.commit()
    db.refresh(fault)
    return fault


def create_alarm_notification(
        db: Session,
        device_id: str,
        message: str,
        notification_type: str = "fault_alarm"
):
    """Persist warning/alarm notifications so server-side history is queryable."""
    notification = Notification(
        device_id=device_id,
        notification_type=notification_type,
        message=message,
        status="unread"
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


def create_upload_error_notification(db: Session, device_id: str, data_type: str, message: str):
    """上传失败时根据规则生成预警通知，并同步到长轮询消息队列。"""
    if not is_upload_error_alarm_enabled(db, data_type):
        return None
    notification = create_alarm_notification(
        db=db,
        device_id=device_id,
        message=f"{data_type} 数据上传错误: {message}",
        notification_type="fault_alarm"
    )
    remember_polling_notification({
        "type": "fault_alarm",
        "device_id": device_id,
        "file_name": None,
        "data_type": data_type,
        "fault_summary": f"上传错误: {message}",
        "timestamp": datetime.now().isoformat(),
    })
    return notification
