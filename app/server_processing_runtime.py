from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.data_processing import analyze_image_file, dumps_json, parse_excel_file
from app.database import (
    SessionLocal,
    MeterExcelData,
    MeterImageData,
    MeterExcelParseResult,
    MeterImageAnalysisResult,
)
from app.feature_services import (
    create_alarm_notification,
    detect_fault_flag,
    evaluate_alarm_rules,
    merge_fault_summary,
    sync_fault_record,
    upsert_search_index,
    remember_polling_notification,
)
from app.logger import logger

PROCESSING_POLL_INTERVAL = 2
processor_stop_event = threading.Event()
excel_processor_thread = None
image_processor_thread = None


def start_processing_workers():
    """启动服务端后台处理线程。"""
    global excel_processor_thread, image_processor_thread
    processor_stop_event.clear()
    if excel_processor_thread is None or not excel_processor_thread.is_alive():
        excel_processor_thread = threading.Thread(target=_excel_worker_loop, name="excel_processor", daemon=True)
        excel_processor_thread.start()
    if image_processor_thread is None or not image_processor_thread.is_alive():
        image_processor_thread = threading.Thread(target=_image_worker_loop, name="image_processor", daemon=True)
        image_processor_thread.start()


def stop_processing_workers():
    """停止服务端后台处理线程。"""
    processor_stop_event.set()
    for thread in (excel_processor_thread, image_processor_thread):
        if thread and thread.is_alive():
            thread.join(timeout=3)


def _excel_worker_loop():
    while not processor_stop_event.is_set():
        processed = process_next_excel_record()
        if not processed:
            processor_stop_event.wait(PROCESSING_POLL_INTERVAL)


def _image_worker_loop():
    while not processor_stop_event.is_set():
        processed = process_next_image_record()
        if not processed:
            processor_stop_event.wait(PROCESSING_POLL_INTERVAL)


def _emit_fault_alarm(db: Session, data_type: str, record, fault_summary: str, fault_id: Optional[int] = None):
    message_prefix = "电量数据预警" if data_type == "excel" else "几何量数据预警"
    create_alarm_notification(
        db=db,
        device_id=record.device_id,
        message=f"{message_prefix}: {record.file_name} - {fault_summary}",
        notification_type="fault_alarm"
    )
    remember_polling_notification({
        "type": "fault_alarm",
        "device_id": record.device_id,
        "file_id": record.id,
        "file_name": record.file_name,
        "file_size": record.file_size,
        "data_type": data_type,
        "fault_id": fault_id,
        "fault_summary": fault_summary,
        "timestamp": datetime.now().isoformat(),
    })


def process_next_excel_record() -> bool:
    db = SessionLocal()
    try:
        record = db.query(MeterExcelData).filter(
            MeterExcelData.processing_status == "pending"
        ).order_by(MeterExcelData.upload_time.asc()).first()
        if not record:
            return False

        record.processing_status = "processing"
        record.processing_error = None
        db.commit()
        db.refresh(record)

        try:
            payload = parse_excel_file(record.file_path)
            parse_result = db.query(MeterExcelParseResult).filter(
                MeterExcelParseResult.excel_id == record.id
            ).first()
            if not parse_result:
                parse_result = MeterExcelParseResult(excel_id=record.id, device_id=record.device_id)
                db.add(parse_result)

            parse_result.device_id = record.device_id
            parse_result.sheet_count = payload.get("sheet_count", 0)
            parse_result.rated_voltage = payload.get("rated_voltage", 0)
            parse_result.rated_voltage_unit = payload.get("rated_voltage_unit", "")
            parse_result.rated_frequency = payload.get("rated_frequency", 0)
            parse_result.rated_frequency_unit = payload.get("rated_frequency_unit", "")
            parse_result.numeric_value_count = payload.get("numeric_value_count", 0)
            parse_result.max_numeric_value = payload.get("max_numeric_value", 0)
            parse_result.min_numeric_value = payload.get("min_numeric_value", 0)
            parse_result.avg_numeric_value = payload.get("avg_numeric_value", 0)
            parse_result.parse_summary = dumps_json(payload.get("summary", {}))
            parse_result.parsed_data_json = dumps_json(payload.get("parsed_data", {}))
            parse_result.processed_at = datetime.now()

            record.processing_status = "done"
            record.processing_error = None
            record.processed_at = datetime.now()
            db.commit()
            db.refresh(record)

            base_fault, base_summary = detect_fault_flag(record.file_name, record.description)
            metrics = {
                "file_size_kb": round((record.file_size or 0) / 1024, 2),
                "sheet_count": payload.get("sheet_count", 0),
                "numeric_value_count": payload.get("numeric_value_count", 0),
                "max_numeric_value": payload.get("max_numeric_value", 0),
                "min_numeric_value": payload.get("min_numeric_value", 0),
                "avg_numeric_value": payload.get("avg_numeric_value", 0),
            }
            rule_messages, rule_severity = evaluate_alarm_rules(db, "excel", metrics)
            detected_fault = base_fault or bool(rule_messages)
            fault_summary = merge_fault_summary(base_summary, rule_messages)
            upsert_search_index(db, "excel", record, record.location, detected_fault, fault_summary)
            fault_record = sync_fault_record(db, "excel", record, detected_fault, fault_summary, severity=rule_severity)
            if detected_fault:
                _emit_fault_alarm(db, "excel", record, fault_summary, fault_record.id if fault_record else None)

            remember_polling_notification({
                "type": "excel_processed",
                "device_id": record.device_id,
                "file_id": record.id,
                "file_name": record.file_name,
                "file_size": record.file_size,
                "data_type": "电量数据",
                "processing_status": "done",
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as exc:
            record.processing_status = "failed"
            record.processing_error = str(exc)
            record.processed_at = datetime.now()
            db.commit()

            metrics = {"processing_failed": 1}
            rule_messages, rule_severity = evaluate_alarm_rules(db, "excel", metrics)
            fault_summary = merge_fault_summary(f"Excel解析失败: {exc}", rule_messages)
            upsert_search_index(db, "excel", record, record.location, True, fault_summary)
            fault_record = sync_fault_record(db, "excel", record, True, fault_summary, severity=rule_severity or "critical")
            _emit_fault_alarm(db, "excel", record, fault_summary, fault_record.id if fault_record else None)
            remember_polling_notification({
                "type": "excel_processed",
                "device_id": record.device_id,
                "file_id": record.id,
                "file_name": record.file_name,
                "file_size": record.file_size,
                "data_type": "电量数据",
                "processing_status": "failed",
                "timestamp": datetime.now().isoformat(),
            })
        return True
    finally:
        db.close()


def process_next_image_record() -> bool:
    db = SessionLocal()
    try:
        record = db.query(MeterImageData).filter(
            MeterImageData.processing_status == "pending"
        ).order_by(MeterImageData.upload_time.asc()).first()
        if not record:
            return False

        record.processing_status = "processing"
        record.processing_error = None
        db.commit()
        db.refresh(record)

        try:
            payload = analyze_image_file(record.file_path, record.file_name, record.description or "")
            analysis_result = db.query(MeterImageAnalysisResult).filter(
                MeterImageAnalysisResult.image_id == record.id
            ).first()
            if not analysis_result:
                analysis_result = MeterImageAnalysisResult(image_id=record.id, device_id=record.device_id)
                db.add(analysis_result)

            analysis_result.device_id = record.device_id
            analysis_result.recognized_path = payload.get("recognized_path")
            analysis_result.image_width = payload.get("image_width", 0)
            analysis_result.image_height = payload.get("image_height", 0)
            analysis_result.image_mode = payload.get("image_mode", "")
            analysis_result.mean_brightness = payload.get("mean_brightness", 0)
            analysis_result.brightness_std = payload.get("brightness_std", 0)
            analysis_result.contrast_score = payload.get("contrast_score", 0)
            analysis_result.sharpness_score = payload.get("sharpness_score", 0)
            analysis_result.dominant_color = payload.get("dominant_color", "")
            analysis_result.has_fault = payload.get("has_fault", False)
            analysis_result.analysis_summary = payload.get("analysis_summary", "")
            analysis_result.analysis_data_json = dumps_json(payload.get("analysis_data", {}))
            analysis_result.processed_at = datetime.now()

            record.processing_status = "done"
            record.processing_error = None
            record.processed_at = datetime.now()
            db.commit()
            db.refresh(record)

            base_fault, base_summary = detect_fault_flag(record.file_name, record.description)
            metrics = {
                "file_size_kb": round((record.file_size or 0) / 1024, 2),
                "original_size_kb": round((record.original_size or 0) / 1024, 2),
                "compression_ratio": round(record.compression_ratio or 0, 2),
                "mean_brightness": payload.get("mean_brightness", 0),
                "sharpness_score": payload.get("sharpness_score", 0),
            }
            rule_messages, rule_severity = evaluate_alarm_rules(db, "image", metrics)
            detected_fault = base_fault or payload.get("has_fault", False) or bool(rule_messages)
            fault_summary = merge_fault_summary(base_summary if base_fault else payload.get("analysis_summary", "图像分析正常"), rule_messages)
            upsert_search_index(db, "image", record, record.location, detected_fault, fault_summary)
            fault_record = sync_fault_record(db, "image", record, detected_fault, fault_summary, severity=rule_severity)
            if detected_fault:
                _emit_fault_alarm(db, "image", record, fault_summary, fault_record.id if fault_record else None)

            remember_polling_notification({
                "type": "image_processed",
                "device_id": record.device_id,
                "file_id": record.id,
                "file_name": record.file_name,
                "file_size": record.file_size,
                "original_size": record.original_size,
                "compression_ratio": record.compression_ratio,
                "data_type": "几何量数据",
                "processing_status": "done",
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as exc:
            record.processing_status = "failed"
            record.processing_error = str(exc)
            record.processed_at = datetime.now()
            db.commit()

            metrics = {"processing_failed": 1}
            rule_messages, rule_severity = evaluate_alarm_rules(db, "image", metrics)
            fault_summary = merge_fault_summary(f"图片分析失败: {exc}", rule_messages)
            upsert_search_index(db, "image", record, record.location, True, fault_summary)
            fault_record = sync_fault_record(db, "image", record, True, fault_summary, severity=rule_severity or "critical")
            _emit_fault_alarm(db, "image", record, fault_summary, fault_record.id if fault_record else None)
            remember_polling_notification({
                "type": "image_processed",
                "device_id": record.device_id,
                "file_id": record.id,
                "file_name": record.file_name,
                "file_size": record.file_size,
                "original_size": record.original_size,
                "compression_ratio": record.compression_ratio,
                "data_type": "几何量数据",
                "processing_status": "failed",
                "timestamp": datetime.now().isoformat(),
            })
        return True
    finally:
        db.close()


def _loads_json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def serialize_excel_record(db: Session, file_record: MeterExcelData, include_parsed_data: bool = False) -> dict:
    parse_result = db.query(MeterExcelParseResult).filter(MeterExcelParseResult.excel_id == file_record.id).first()
    summary = _loads_json(parse_result.parse_summary) if parse_result else None
    parsed_data = _loads_json(parse_result.parsed_data_json) if (include_parsed_data and parse_result) else None
    return {
        "id": file_record.id,
        "device_id": file_record.device_id,
        "file_name": file_record.file_name,
        "file_path": file_record.file_path,
        "file_size": file_record.file_size,
        "location": file_record.location,
        "upload_time": file_record.upload_time.isoformat() if file_record.upload_time else None,
        "description": file_record.description,
        "processing_status": file_record.processing_status,
        "processing_error": file_record.processing_error,
        "processed_at": file_record.processed_at.isoformat() if file_record.processed_at else None,
        "download_url": f"/api/file/download/excel/{file_record.id}",
        "parse_result": {
            "sheet_count": parse_result.sheet_count if parse_result else 0,
            "rated_voltage": parse_result.rated_voltage if parse_result else 0,
            "rated_voltage_unit": parse_result.rated_voltage_unit if parse_result else "",
            "rated_frequency": parse_result.rated_frequency if parse_result else 0,
            "rated_frequency_unit": parse_result.rated_frequency_unit if parse_result else "",
            "numeric_value_count": parse_result.numeric_value_count if parse_result else 0,
            "max_numeric_value": parse_result.max_numeric_value if parse_result else 0,
            "min_numeric_value": parse_result.min_numeric_value if parse_result else 0,
            "avg_numeric_value": parse_result.avg_numeric_value if parse_result else 0,
            "summary": summary or {},
            "parsed_data": parsed_data,
        }
    }


def serialize_image_record(db: Session, file_record: MeterImageData, include_analysis_data: bool = False) -> dict:
    analysis_result = db.query(MeterImageAnalysisResult).filter(MeterImageAnalysisResult.image_id == file_record.id).first()
    analysis_data = _loads_json(analysis_result.analysis_data_json) if (include_analysis_data and analysis_result) else None
    return {
        "id": file_record.id,
        "device_id": file_record.device_id,
        "file_name": file_record.file_name,
        "file_path": file_record.file_path,
        "file_size": file_record.file_size,
        "original_size": file_record.original_size,
        "compression_ratio": file_record.compression_ratio,
        "location": file_record.location,
        "upload_time": file_record.upload_time.isoformat() if file_record.upload_time else None,
        "description": file_record.description,
        "image_type": file_record.image_type,
        "processing_status": file_record.processing_status,
        "processing_error": file_record.processing_error,
        "processed_at": file_record.processed_at.isoformat() if file_record.processed_at else None,
        "download_url": f"/api/file/download/image/{file_record.id}",
        "analysis_result": {
            "recognized_path": analysis_result.recognized_path if analysis_result else None,
            "image_width": analysis_result.image_width if analysis_result else 0,
            "image_height": analysis_result.image_height if analysis_result else 0,
            "image_mode": analysis_result.image_mode if analysis_result else "",
            "mean_brightness": analysis_result.mean_brightness if analysis_result else 0,
            "brightness_std": analysis_result.brightness_std if analysis_result else 0,
            "contrast_score": analysis_result.contrast_score if analysis_result else 0,
            "sharpness_score": analysis_result.sharpness_score if analysis_result else 0,
            "dominant_color": analysis_result.dominant_color if analysis_result else "",
            "has_fault": analysis_result.has_fault if analysis_result else False,
            "analysis_summary": analysis_result.analysis_summary if analysis_result else "",
            "analysis_data": analysis_data,
        }
    }
