"""不落正式文件记录的报警延迟性能测试服务。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import uuid

from app.data_processing import analyze_image_file, parse_excel_alarm_metrics
from app.database import SessionLocal
from app.feature_services import evaluate_alarm_rules, merge_fault_summary
from app.logger import logger


TEST_DEVICE_ID = "upper_client"
EXCEL_SUFFIXES = {".xlsx", ".xls"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


def _excel_alarm_metrics(file_path, file_size: int) -> dict:
    """读取测试专用的轻量指标，不构造上位机图表解析数据。"""
    metrics = dict(parse_excel_alarm_metrics(file_path))
    metrics["file_size_kb"] = round(file_size / 1024, 2)
    return metrics
def _image_alarm_metrics(payload: dict, file_size: int) -> dict:
    return {
        "file_size_kb": round(file_size / 1024, 2),
        "original_size_kb": round(file_size / 1024, 2),
        "compression_ratio": 100.0,
        "mean_brightness": payload.get("mean_brightness", 0),
        "sharpness_score": payload.get("sharpness_score", 0),
    }


def process_alarm_latency_test(file_bytes: bytes, file_name: str) -> dict:
    """临时保存、分析并删除一个文件，只在命中规则时创建预警通知。"""
    safe_name = Path(file_name or "test_file").name
    suffix = Path(safe_name).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        data_type = "excel"
        data_type_text = "电量数据"
    elif suffix in IMAGE_SUFFIXES:
        data_type = "image"
        data_type_text = "几何量数据"
    else:
        raise ValueError("仅支持 xlsx、xls、jpg、jpeg、png、bmp 文件")

    test_id = uuid.uuid4().hex
    started_ns = time.time_ns()
    db = SessionLocal()
    processing_status = "done"
    processing_error = None
    alarm_triggered = False
    alarm_message = "规则判断完成，未触发预警"
    severity = "warning"

    try:
        with TemporaryDirectory(prefix="nqi_alarm_latency_") as temp_dir:
            temp_path = Path(temp_dir) / f"{test_id}{suffix}"
            temp_path.write_bytes(file_bytes)
            logger.info(
                f"[alarm-latency-test] temporary file saved: test_id={test_id}, "
                f"type={data_type}, name={safe_name}, size={len(file_bytes)}, path={temp_path}"
            )
            try:
                if data_type == "excel":
                    metrics = _excel_alarm_metrics(temp_path, len(file_bytes))
                    rule_messages, severity = evaluate_alarm_rules(db, "excel", metrics)
                    alarm_triggered = bool(rule_messages)
                    alarm_message = merge_fault_summary("", rule_messages) or alarm_message
                else:
                    # 测试只依据图像内容和规则，不依据文件名关键字制造预警。
                    payload = analyze_image_file(temp_path, "", "")
                    metrics = _image_alarm_metrics(payload, len(file_bytes))
                    rule_messages, severity = evaluate_alarm_rules(db, "image", metrics)
                    alarm_triggered = bool(payload.get("has_fault")) or bool(rule_messages)
                    base_message = payload.get("analysis_summary", "图像分析正常")
                    alarm_message = merge_fault_summary(
                        base_message if alarm_triggered else "", rule_messages
                    ) or alarm_message
            except Exception as exc:
                db.rollback()
                processing_status = "failed"
                processing_error = str(exc)
                rule_messages, severity = evaluate_alarm_rules(
                    db, data_type, {"processing_failed": 1}
                )
                alarm_triggered = True
                alarm_message = merge_fault_summary(
                    f"{data_type_text}测试解析失败: {exc}", rule_messages
                )


        # 测试结果不写通知库或消息队列；临时目录删除后直接返回给当前上位机。
        finished_ns = time.time_ns()
        latency_ms = round((finished_ns - started_ns) / 1_000_000, 3)
        logger.info(
            f"[alarm-latency-test] completed and temporary file deleted: "
            f"test_id={test_id}, type={data_type}, alarm_triggered={alarm_triggered}, "
            f"status={processing_status}, alarm_latency_ms={latency_ms:.3f}, "
            f"target_met={latency_ms < 1000.0}"
        )
        return {
            "status": "success",
            "test_id": test_id,
            "device_id": TEST_DEVICE_ID,
            "data_type": data_type,
            "data_type_text": data_type_text,
            "file_name": safe_name,
            "file_record_persisted": False,
            "notification_persisted": False,
            "queue_message_published": False,
            "temporary_file_deleted": True,
            "processing_status": processing_status,
            "processing_error": processing_error,
            "alarm_evaluated": True,
            "alarm_triggered": alarm_triggered,
            "alarm_latency_ms": latency_ms,
            "alarm_target_ms": 1000.0,
            "alarm_target_met": latency_ms < 1000.0,
            "alarm_info": {"message": alarm_message, "severity": severity},
        }
    finally:
        db.close()

