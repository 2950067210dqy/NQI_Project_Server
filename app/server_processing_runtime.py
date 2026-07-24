from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.data_processing import analyze_image_file, dumps_json, parse_excel_file
from app.database import (
    SessionLocal,
    MeterExcelData,
    MeterImageData,
    MeterExcelParseResult,
    MeterExcelMeasurementDetail,
    MeterExcelErrorDetail,
    MeterImageAnalysisResult,
    FaultRecord,
    DataSearchIndex,
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
from app.config_ini import config
from app.logger import logger

PROCESSING_POLL_INTERVAL = config.processing_poll_interval
PROCESSOR_SUPERVISOR_INTERVAL = config.processing_supervisor_interval
PROCESSOR_HEARTBEAT_TIMEOUT = config.processing_heartbeat_timeout
PROCESSOR_ERROR_BACKOFF_MAX = config.processing_error_backoff_max
PROCESSOR_STATUS_LOG_INTERVAL = config.processing_status_log_interval

processor_stop_event = threading.Event()
processor_lock = threading.RLock()
excel_processor_thread = None
image_processor_thread = None
processor_supervisor_thread = None
_worker_wake_events = {
    "excel": threading.Event(),
    "image": threading.Event(),
}

_worker_states = {
    "excel": {
        "status": "not_started",
        "last_heartbeat": None,
        "_heartbeat_monotonic": 0.0,
        "processed_cycles": 0,
        "error_count": 0,
        "consecutive_errors": 0,
        "restart_count": 0,
        "last_error": None,
        "current_task": None,
        "stale_reported": False,
    },
    "image": {
        "status": "not_started",
        "last_heartbeat": None,
        "_heartbeat_monotonic": 0.0,
        "processed_cycles": 0,
        "error_count": 0,
        "consecutive_errors": 0,
        "restart_count": 0,
        "last_error": None,
        "current_task": None,
        "stale_reported": False,
    },
}


def _thread_for_worker(worker_name: str):
    return excel_processor_thread if worker_name == "excel" else image_processor_thread


def _set_thread_for_worker(worker_name: str, thread):
    global excel_processor_thread, image_processor_thread
    if worker_name == "excel":
        excel_processor_thread = thread
    else:
        image_processor_thread = thread


def _update_worker_state(worker_name: str, **changes):
    now_monotonic = time.monotonic()
    with processor_lock:
        state = _worker_states[worker_name]
        state.update(changes)
        state["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")
        state["_heartbeat_monotonic"] = now_monotonic
        state["stale_reported"] = False


def _start_worker_locked(worker_name: str, reason: str):
    target = _excel_worker_loop if worker_name == "excel" else _image_worker_loop
    state = _worker_states[worker_name]
    if reason != "server_startup":
        state["restart_count"] += 1
    state["status"] = "starting"
    state["last_error"] = None
    thread = threading.Thread(
        target=target,
        name=f"{worker_name}_processor",
        daemon=True,
    )
    _set_thread_for_worker(worker_name, thread)
    thread.start()
    logger.info(
        f"[processor-supervisor] worker started: worker={worker_name}, "
        f"reason={reason}, thread_name={thread.name}, thread_id={thread.ident}, "
        f"restart_count={state['restart_count']}"
    )


def _ensure_worker_locked(worker_name: str, reason: str):
    thread = _thread_for_worker(worker_name)
    if thread is None or not thread.is_alive():
        _start_worker_locked(worker_name, reason)


def start_processing_workers():
    """Start parsing workers and the watchdog that keeps them alive."""
    global processor_supervisor_thread
    processor_stop_event.clear()
    # A previous graceful shutdown sets these events to release waiting workers.
    # Clear them before restart so workers wait for a real upload notification.
    for wake_event in _worker_wake_events.values():
        wake_event.clear()
    with processor_lock:
        _ensure_worker_locked("excel", "server_startup")
        _ensure_worker_locked("image", "server_startup")
        if processor_supervisor_thread is None or not processor_supervisor_thread.is_alive():
            processor_supervisor_thread = threading.Thread(
                target=_processor_supervisor_loop,
                name="processor_supervisor",
                daemon=True,
            )
            processor_supervisor_thread.start()
            logger.info(
                f"[processor-supervisor] watchdog started: "
                f"thread_id={processor_supervisor_thread.ident}, "
                f"check_interval={PROCESSOR_SUPERVISOR_INTERVAL}s, "
                f"heartbeat_timeout={PROCESSOR_HEARTBEAT_TIMEOUT}s"
            )


def stop_processing_workers():
    """Stop the watchdog and both parsing workers gracefully."""
    global excel_processor_thread, image_processor_thread, processor_supervisor_thread
    logger.info("[processor-supervisor] shutdown requested")
    processor_stop_event.set()
    for wake_event in _worker_wake_events.values():
        wake_event.set()
    threads = (processor_supervisor_thread, excel_processor_thread, image_processor_thread)
    for thread in threads:
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning(
                    f"[processor-supervisor] thread did not stop within timeout: "
                    f"name={thread.name}, thread_id={thread.ident}"
                )
    with processor_lock:
        processor_supervisor_thread = None
        excel_processor_thread = None
        image_processor_thread = None
    logger.info("[processor-supervisor] shutdown completed")


def _excel_processing_cycle() -> bool:
    processed = process_next_excel_record()
    if not processed:
        processed = process_next_excel_detail_backfill()
    return processed


def _image_processing_cycle() -> bool:
    return process_next_image_record()


def notify_processing_worker(data_type: str, file_id: int = None):
    """Wake the matching worker immediately after an upload is committed."""
    worker_name = "excel" if str(data_type).lower() == "excel" else "image"
    _worker_wake_events[worker_name].set()
    logger.info(
        f"[processor-dispatch] immediate wake requested: "
        f"worker={worker_name}, file_id={file_id}"
    )


def _wait_for_worker_signal(worker_name: str, timeout: float):
    wake_event = _worker_wake_events[worker_name]
    wake_event.wait(timeout)
    wake_event.clear()


def _complete_alarm_timing(record, alarm_triggered: bool) -> float:
    """Persist rule-evaluation latency using a cross-process nanosecond clock."""
    finished_ns = time.time_ns()
    started_ns = getattr(record, "alarm_clock_started_ns", None)
    if not started_ns:
        upload_time = getattr(record, "upload_time", None)
        started_ns = int(upload_time.timestamp() * 1_000_000_000) if upload_time else finished_ns
    latency_ms = max(0.0, (finished_ns - int(started_ns)) / 1_000_000)
    record.alarm_clock_started_ns = int(started_ns)
    record.alarm_clock_finished_ns = finished_ns
    record.alarm_latency_ms = round(latency_ms, 3)
    record.alarm_triggered = bool(alarm_triggered)
    return record.alarm_latency_ms


def _run_worker_loop(worker_name: str, process_cycle):
    thread = threading.current_thread()
    _update_worker_state(worker_name, status="running")
    logger.info(
        f"[{thread.name}] loop entered: thread_id={thread.ident}, "
        f"poll_interval={PROCESSING_POLL_INTERVAL}s"
    )
    try:
        while not processor_stop_event.is_set():
            _update_worker_state(worker_name, status="checking", current_task=None)
            try:
                processed = bool(process_cycle())
            except Exception as exc:
                with processor_lock:
                    state = _worker_states[worker_name]
                    state["status"] = "retry_wait"
                    state["error_count"] += 1
                    state["consecutive_errors"] += 1
                    state["last_error"] = f"{type(exc).__name__}: {exc}"
                    state["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")
                    state["_heartbeat_monotonic"] = time.monotonic()
                    consecutive_errors = state["consecutive_errors"]
                backoff = min(
                    PROCESSOR_ERROR_BACKOFF_MAX,
                    max(PROCESSING_POLL_INTERVAL, 2 ** min(consecutive_errors - 1, 5)),
                )
                logger.opt(exception=True).error(
                    f"[{thread.name}] worker cycle failed but loop remains alive: "
                    f"error_type={type(exc).__name__}, error={exc}, "
                    f"consecutive_errors={consecutive_errors}, retry_in={backoff}s"
                )
                processor_stop_event.wait(backoff)
                continue
            except BaseException as exc:
                with processor_lock:
                    state = _worker_states[worker_name]
                    state["status"] = "crashed"
                    state["last_error"] = f"{type(exc).__name__}: {exc}"
                    state["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")
                    state["_heartbeat_monotonic"] = time.monotonic()
                logger.opt(exception=True).critical(
                    f"[{thread.name}] worker terminated by non-standard exception: "
                    f"error_type={type(exc).__name__}, error={exc}; watchdog will restart it"
                )
                raise

            with processor_lock:
                state = _worker_states[worker_name]
                state["consecutive_errors"] = 0
                state["last_error"] = None
                if processed:
                    state["processed_cycles"] += 1
            if processed:
                _update_worker_state(worker_name, status="processing_next")
            else:
                _update_worker_state(worker_name, status="idle")
                _wait_for_worker_signal(worker_name, PROCESSING_POLL_INTERVAL)
    finally:
        status = "stopped" if processor_stop_event.is_set() else "crashed"
        _update_worker_state(worker_name, status=status)
        log_method = logger.info if processor_stop_event.is_set() else logger.error
        log_method(
            f"[{thread.name}] loop exited: thread_id={thread.ident}, status={status}"
        )


def _excel_worker_loop():
    _run_worker_loop("excel", _excel_processing_cycle)


def _image_worker_loop():
    _run_worker_loop("image", _image_processing_cycle)


def _processing_queue_snapshot() -> dict:
    db = SessionLocal()
    try:
        snapshot = {}
        for worker_name, model in (("excel", MeterExcelData), ("image", MeterImageData)):
            snapshot[worker_name] = {
                status: db.query(model).filter(model.processing_status == status).count()
                for status in ("pending", "processing", "done", "failed")
            }
        return snapshot
    finally:
        db.close()


def _log_processor_status():
    status = get_processing_worker_status()
    try:
        queue = _processing_queue_snapshot()
    except Exception as exc:
        logger.opt(exception=True).error(
            f"[processor-supervisor] failed to read queue snapshot: "
            f"error_type={type(exc).__name__}, error={exc}"
        )
        queue = {"excel": "unavailable", "image": "unavailable"}

    logger.info(
        f"[processor-supervisor] health report: "
        f"excel={status['workers']['excel']}, excel_queue={queue['excel']}; "
        f"image={status['workers']['image']}, image_queue={queue['image']}"
    )


def _processor_supervisor_loop():
    logger.info("[processor-supervisor] watchdog loop entered")
    next_status_log = time.monotonic()
    try:
        while not processor_stop_event.wait(PROCESSOR_SUPERVISOR_INTERVAL):
            try:
                with processor_lock:
                    for worker_name in ("excel", "image"):
                        thread = _thread_for_worker(worker_name)
                        state = _worker_states[worker_name]
                        if thread is None or not thread.is_alive():
                            logger.error(
                                f"[processor-supervisor] worker offline, restarting: "
                                f"worker={worker_name}, previous_status={state['status']}, "
                                f"last_heartbeat={state['last_heartbeat']}, "
                                f"last_error={state['last_error']}"
                            )
                            _start_worker_locked(worker_name, "worker_offline")
                            continue

                        heartbeat_age = time.monotonic() - state["_heartbeat_monotonic"]
                        if (
                            state["_heartbeat_monotonic"] > 0
                            and heartbeat_age > PROCESSOR_HEARTBEAT_TIMEOUT
                            and not state["stale_reported"]
                        ):
                            state["stale_reported"] = True
                            logger.critical(
                                f"[processor-supervisor] worker heartbeat is stale: "
                                f"worker={worker_name}, thread_id={thread.ident}, "
                                f"status={state['status']}, heartbeat_age={heartbeat_age:.1f}s. "
                                f"The thread is alive but may be blocked in file or database I/O."
                            )

                if time.monotonic() >= next_status_log:
                    _log_processor_status()
                    next_status_log = time.monotonic() + PROCESSOR_STATUS_LOG_INTERVAL
            except Exception as exc:
                logger.opt(exception=True).error(
                    f"[processor-supervisor] watchdog check failed; monitoring continues: "
                    f"error_type={type(exc).__name__}, error={exc}"
                )
    finally:
        logger.info("[processor-supervisor] watchdog loop exited")


def get_processing_worker_status() -> dict:
    """Return a JSON-serializable snapshot for operations and health checks."""
    now = time.monotonic()
    with processor_lock:
        workers = {}
        for worker_name in ("excel", "image"):
            thread = _thread_for_worker(worker_name)
            state = _worker_states[worker_name]
            heartbeat_monotonic = state["_heartbeat_monotonic"]
            workers[worker_name] = {
                key: value
                for key, value in state.items()
                if not key.startswith("_") and key != "stale_reported"
            }
            workers[worker_name].update({
                "alive": bool(thread and thread.is_alive()),
                "thread_name": thread.name if thread else None,
                "thread_id": thread.ident if thread else None,
                "heartbeat_age_seconds": (
                    round(max(0.0, now - heartbeat_monotonic), 3)
                    if heartbeat_monotonic
                    else None
                ),
            })
        supervisor = processor_supervisor_thread
        return {
            "stop_requested": processor_stop_event.is_set(),
            "supervisor_alive": bool(supervisor and supervisor.is_alive()),
            "supervisor_thread_id": supervisor.ident if supervisor else None,
            "workers": workers,
        }



EXCEL_METRIC_KEY_MAP = {
    "功率W": "power_w",
    "功率": "power_w",
    "电压": "voltage",
    "电流": "current",
    "相角": "phase_angle",
}


def _safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _excel_metric_key(metric_name: str) -> str:
    """把中文指标名称转成稳定 metric_key，供预警规则复用。"""
    metric_name = str(metric_name or "").strip()
    if metric_name in EXCEL_METRIC_KEY_MAP:
        return EXCEL_METRIC_KEY_MAP[metric_name]
    return metric_name.lower().replace(" ", "_") or "unknown"


def _summarize_values(values: list[float]) -> dict:
    """计算一组明细值的基础统计量。"""
    if not values:
        return {"count": 0, "max": None, "min": None, "avg": None}
    return {
        "count": len(values),
        "max": max(values),
        "min": min(values),
        "avg": sum(values) / len(values),
    }


def _scope_key(value) -> str:
    """把 Sheet/相位名称转成适合作为预警 metric_key 片段的短键。"""
    text = str(value or "").strip().replace(" ", "_")
    return text.replace("相", "") or "unknown"


def _abs_summary(values: list[float]) -> dict:
    """误差预警按绝对值统计，避免负误差漏报。"""
    return _summarize_values([abs(value) for value in values if value is not None])


def _build_excel_detail_rows_and_metrics(
    payload: dict,
    record: MeterExcelData,
    parse_result_id: int,
    include_rows: bool = True,
):
    """把 parsed_data_json 进一步拆成行级图表柱值和误差明细，并生成精细预警指标。"""
    rows = []
    error_rows = []
    row_count = 0
    error_row_count = 0
    values_by_metric = defaultdict(list)
    values_by_sheet_metric = defaultdict(lambda: defaultdict(list))
    errors_by_metric_percent = defaultdict(list)
    errors_by_metric_ppm = defaultdict(list)
    errors_by_sheet_metric_percent = defaultdict(lambda: defaultdict(list))
    errors_by_sheet_metric_ppm = defaultdict(lambda: defaultdict(list))
    errors_by_sheet_phase_metric_percent = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    errors_by_sheet_phase_metric_ppm = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    parsed_data = payload.get("parsed_data") or {}
    processed_at = datetime.now()

    for sheet_name, sheet_payload in parsed_data.items():
        rated_voltage = _safe_float(sheet_payload.get("rated_voltage"))
        for metric_block in sheet_payload.get("data", []) or []:
            metric_name = metric_block.get("name", "")
            metric_key = metric_block.get("metric_key") or _excel_metric_key(metric_name)
            metric_group_index = int(metric_block.get("metric_group_index") or 0)
            value_unit = metric_block.get("value_unit") or ""
            metric_data = metric_block.get("data", {}) or {}
            x_payload = metric_data.get("x", {}) or {}
            x_points = x_payload.get("data", []) or []
            point_meta = x_payload.get("point_meta", []) or []
            for phase_index, phase_block in enumerate(metric_data.get("y", []) or []):
                phase_name = phase_block.get("name", "")
                phase_index = int(phase_block.get("phase_index", phase_index) or 0)
                for meter_index, meter_block in enumerate(phase_block.get("data", []) or []):
                    meter_name = meter_block.get("name", "")
                    meter_index = int(meter_block.get("meter_index", meter_index) or 0)
                    for point_index, raw_value in enumerate(meter_block.get("data", []) or []):
                        value = _safe_float(raw_value)
                        if value is None:
                            continue
                        x_point = x_points[point_index] if point_index < len(x_points) else []
                        meta = point_meta[point_index] if point_index < len(point_meta) else {}
                        x_angle = _safe_float(meta.get("phase_angle_degree"))
                        x_current = _safe_float(meta.get("rated_current_a"))
                        if x_angle is None and isinstance(x_point, (list, tuple)) and len(x_point) > 0:
                            x_angle = _safe_float(x_point[0])
                        if x_current is None and isinstance(x_point, (list, tuple)) and len(x_point) > 1:
                            x_current = _safe_float(x_point[1])
                        values_by_metric[metric_key].append(value)
                        values_by_sheet_metric[str(sheet_name)][metric_key].append(value)
                        row_count += 1
                        if include_rows:
                            rows.append(MeterExcelMeasurementDetail(
                                excel_id=record.id,
                                parse_result_id=parse_result_id,
                                device_id=record.device_id,
                                sheet_name=str(sheet_name),
                                metric_group_index=metric_group_index,
                                metric_name=str(metric_name),
                                metric_key=metric_key,
                                phase_name=str(phase_name),
                                phase_index=phase_index,
                                meter_name=str(meter_name),
                                meter_index=meter_index,
                                point_index=int(meta.get("point_index", point_index) or 0),
                                source_excel_row=meta.get("source_excel_row"),
                                range_text=meta.get("range_text"),
                                frequency_hz=_safe_float(meta.get("frequency_hz")),
                                rated_voltage_v=_safe_float(meta.get("rated_voltage_v")) or rated_voltage,
                                rated_current_a=x_current,
                                x_angle_degree=x_angle,
                                x_current_a=x_current,
                                value_unit=str(value_unit),
                                chart_series_name=f"{phase_name}-{meter_name}",
                                value=value,
                                processed_at=processed_at,
                            ))

        for error_item in sheet_payload.get("error_data", []) or []:
            metric_name = error_item.get("metric_name", "")
            metric_key = error_item.get("metric_key") or _excel_metric_key(metric_name)
            error_percent = _safe_float(error_item.get("error_percent"))
            error_ppm = _safe_float(error_item.get("error_ppm"))
            phase_name = str(error_item.get("phase_name", ""))
            sheet_key = _scope_key(sheet_name)
            phase_key = _scope_key(phase_name)
            if error_percent is not None:
                errors_by_metric_percent[metric_key].append(error_percent)
                errors_by_sheet_metric_percent[str(sheet_name)][metric_key].append(error_percent)
                errors_by_sheet_phase_metric_percent[sheet_key][phase_key][metric_key].append(error_percent)
            if error_ppm is not None:
                errors_by_metric_ppm[metric_key].append(error_ppm)
                errors_by_sheet_metric_ppm[str(sheet_name)][metric_key].append(error_ppm)
                errors_by_sheet_phase_metric_ppm[sheet_key][phase_key][metric_key].append(error_ppm)
            error_row_count += 1
            if include_rows:
                error_rows.append(MeterExcelErrorDetail(
                    excel_id=record.id,
                    parse_result_id=parse_result_id,
                    device_id=record.device_id,
                    sheet_name=str(sheet_name),
                    metric_group_index=int(error_item.get("metric_group_index") or 0),
                    metric_name=str(metric_name),
                    metric_key=metric_key,
                    phase_name=phase_name,
                    phase_index=int(error_item.get("phase_index") or 0),
                    point_index=int(error_item.get("point_index") or 0),
                    source_excel_row=error_item.get("source_excel_row"),
                    range_text=error_item.get("range_text"),
                    frequency_hz=_safe_float(error_item.get("frequency_hz")),
                    rated_voltage_v=_safe_float(error_item.get("rated_voltage_v")),
                    rated_current_a=_safe_float(error_item.get("rated_current_a")),
                    x_angle_degree=_safe_float(error_item.get("phase_angle_degree")),
                    x_current_a=_safe_float(error_item.get("rated_current_a")),
                    reference_meter_name=str(error_item.get("reference_meter_name", "")),
                    compared_meter_name=str(error_item.get("compared_meter_name", "")),
                    reference_value=_safe_float(error_item.get("reference_value")),
                    compared_value=_safe_float(error_item.get("compared_value")),
                    error_percent=error_percent,
                    error_ppm=error_ppm,
                    processed_at=processed_at,
                ))

    detail_summary = {
        "metrics": {metric_key: _summarize_values(values) for metric_key, values in values_by_metric.items()},
        "sheets": {
            sheet_name: {metric_key: _summarize_values(values) for metric_key, values in metric_map.items()}
            for sheet_name, metric_map in values_by_sheet_metric.items()
        },
        "errors": {
            metric_key: {
                "error_percent_abs": _abs_summary(errors_by_metric_percent.get(metric_key, [])),
                "error_ppm_abs": _abs_summary(errors_by_metric_ppm.get(metric_key, [])),
            }
            for metric_key in set(errors_by_metric_percent.keys()) | set(errors_by_metric_ppm.keys())
        },
        "row_count": row_count,
        "error_row_count": error_row_count,
    }
    alarm_metrics = {}
    for metric_key, summary in detail_summary["metrics"].items():
        for stat_name in ("max", "min", "avg", "count"):
            value = summary.get(stat_name)
            if value is not None:
                alarm_metrics[f"{metric_key}_{stat_name}"] = value
    # 同时生成按 Sheet 聚合的指标，例如 sheet_A_power_w_max，支持按表页做阈值预警。
    for sheet_name, metric_map in detail_summary["sheets"].items():
        sheet_key = _scope_key(sheet_name)
        for metric_key, summary in metric_map.items():
            for stat_name in ("max", "min", "avg", "count"):
                value = summary.get(stat_name)
                if value is not None:
                    alarm_metrics[f"sheet_{sheet_key}_{metric_key}_{stat_name}"] = value

    for metric_key in set(errors_by_metric_percent.keys()) | set(errors_by_metric_ppm.keys()):
        percent_summary = _abs_summary(errors_by_metric_percent.get(metric_key, []))
        ppm_summary = _abs_summary(errors_by_metric_ppm.get(metric_key, []))
        if percent_summary.get("max") is not None:
            alarm_metrics[f"{metric_key}_error_percent_abs_max"] = percent_summary["max"]
            alarm_metrics[f"{metric_key}_error_percent_abs_avg"] = percent_summary["avg"]
        if ppm_summary.get("max") is not None:
            alarm_metrics[f"{metric_key}_error_ppm_abs_max"] = ppm_summary["max"]
            alarm_metrics[f"{metric_key}_error_ppm_abs_avg"] = ppm_summary["avg"]
    for sheet_name, metric_map in errors_by_sheet_metric_percent.items():
        sheet_key = _scope_key(sheet_name)
        for metric_key, values in metric_map.items():
            summary = _abs_summary(values)
            if summary.get("max") is not None:
                alarm_metrics[f"sheet_{sheet_key}_{metric_key}_error_percent_abs_max"] = summary["max"]
    for sheet_name, metric_map in errors_by_sheet_metric_ppm.items():
        sheet_key = _scope_key(sheet_name)
        for metric_key, values in metric_map.items():
            summary = _abs_summary(values)
            if summary.get("max") is not None:
                alarm_metrics[f"sheet_{sheet_key}_{metric_key}_error_ppm_abs_max"] = summary["max"]
    for sheet_key, phase_map in errors_by_sheet_phase_metric_percent.items():
        for phase_key, metric_map in phase_map.items():
            for metric_key, values in metric_map.items():
                summary = _abs_summary(values)
                if summary.get("max") is not None:
                    alarm_metrics[f"sheet_{sheet_key}_phase_{phase_key}_{metric_key}_error_percent_abs_max"] = summary["max"]
    for sheet_key, phase_map in errors_by_sheet_phase_metric_ppm.items():
        for phase_key, metric_map in phase_map.items():
            for metric_key, values in metric_map.items():
                summary = _abs_summary(values)
                if summary.get("max") is not None:
                    alarm_metrics[f"sheet_{sheet_key}_phase_{phase_key}_{metric_key}_error_ppm_abs_max"] = summary["max"]
    return rows, error_rows, detail_summary, alarm_metrics

def _emit_fault_alarm(db: Session, data_type: str, record, fault_summary: str, fault_id: Optional[int] = None):
    message_prefix = "电量数据预警" if data_type == "excel" else "几何量数据预警"
    create_alarm_notification(
        db=db,
        device_id=record.device_id,
        message=f"{message_prefix}: {record.file_name} - {fault_summary}",
        notification_type="fault_alarm",
        data_type=data_type,
        file_id=record.id,
        file_name=record.file_name,
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
            MeterExcelData.processing_status.in_(("pending", "processing"))
        ).order_by(MeterExcelData.upload_time.asc()).first()
        if not record:
            return False

        record.processing_status = "processing"
        record.processing_error = None
        db.commit()
        db.refresh(record)
        record_id = record.id
        task_started = time.monotonic()
        _update_worker_state(
            "excel",
            status="processing",
            current_task={
                "id": record.id,
                "device_id": record.device_id,
                "file_name": record.file_name,
                "file_path": record.file_path,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        logger.info(
            f"[excel_processor] task claimed: id={record.id}, device_id={record.device_id}, "
            f"file_name={record.file_name}, file_path={record.file_path}, "
            f"file_size={record.file_size}, upload_time={record.upload_time}"
        )

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
            parse_result.chart_point_count = payload.get("chart_point_count", 0)
            parse_result.chart_value_count = payload.get("chart_value_count", 0)
            parse_result.error_value_count = payload.get("error_value_count", 0)
            parse_result.max_numeric_value = payload.get("max_numeric_value", 0)
            parse_result.min_numeric_value = payload.get("min_numeric_value", 0)
            parse_result.avg_numeric_value = payload.get("avg_numeric_value", 0)
            parse_result.parse_summary = dumps_json(payload.get("summary", {}))
            parse_result.parsed_data_json = dumps_json(_strip_excel_parsed_data_for_display(payload.get("parsed_data", {})))
            parse_result.processed_at = datetime.now()
            db.flush()

            # 将大 JSON 中的 Sheet/指标/相位/设备/测试点拆成行级明细，便于服务器端精细预警。
            detail_rows, error_rows, detail_summary, detail_alarm_metrics = _build_excel_detail_rows_and_metrics(payload, record, parse_result.id)
            db.query(MeterExcelMeasurementDetail).filter(MeterExcelMeasurementDetail.excel_id == record.id).delete()
            db.query(MeterExcelErrorDetail).filter(MeterExcelErrorDetail.excel_id == record.id).delete()
            if detail_rows:
                db.bulk_save_objects(detail_rows)
            if error_rows:
                db.bulk_save_objects(error_rows)
            parse_result.chart_point_count = payload.get("chart_point_count", 0) or detail_summary.get("row_count", 0)
            parse_result.chart_value_count = detail_summary.get("row_count", 0)
            parse_result.error_value_count = detail_summary.get("error_row_count", 0)
            parse_result.detail_summary_json = dumps_json(detail_summary)

            record.processing_status = "processing"
            record.processing_error = None
            record.processed_at = None
            db.commit()
            db.refresh(record)

            base_fault, base_summary = detect_fault_flag(record.file_name, record.description)
            metrics = {
                "file_size_kb": round((record.file_size or 0) / 1024, 2),
                "sheet_count": payload.get("sheet_count", 0),
                "numeric_value_count": payload.get("numeric_value_count", 0),
                "chart_value_count": detail_summary.get("row_count", 0),
                "error_value_count": detail_summary.get("error_row_count", 0),
                "max_numeric_value": payload.get("max_numeric_value", 0),
                "min_numeric_value": payload.get("min_numeric_value", 0),
                "avg_numeric_value": payload.get("avg_numeric_value", 0),
            }
            metrics.update(detail_alarm_metrics)
            rule_messages, rule_severity = evaluate_alarm_rules(db, "excel", metrics)
            detected_fault = base_fault or bool(rule_messages)
            fault_summary = merge_fault_summary(base_summary, rule_messages)
            upsert_search_index(db, "excel", record, record.location, detected_fault, fault_summary)
            fault_record = sync_fault_record(db, "excel", record, detected_fault, fault_summary, severity=rule_severity)
            if detected_fault:
                _emit_fault_alarm(db, "excel", record, fault_summary, fault_record.id if fault_record else None)
            alarm_latency_ms = _complete_alarm_timing(record, detected_fault)
            record.processing_status = "done"
            record.processed_at = datetime.now()
            # 预警记录、通知和延迟结果属于同一处理结果，统一提交后才对客户端显示 done。
            db.commit()
            logger.info(
                f"[excel_processor] alarm evaluation completed: id={record.id}, "
                f"alarm_triggered={detected_fault}, alarm_latency_ms={alarm_latency_ms:.3f}, "
                f"target_met={alarm_latency_ms < 1000.0}"
            )

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
            logger.info(
                f"[excel_processor] task completed: id={record.id}, device_id={record.device_id}, "
                f"elapsed={time.monotonic() - task_started:.3f}s, "
                f"sheet_count={payload.get('sheet_count', 0)}, "
                f"detail_rows={detail_summary.get('row_count', 0)}, "
                f"error_rows={detail_summary.get('error_row_count', 0)}, "
                f"detected_fault={detected_fault}"
            )
        except Exception as exc:
            logger.opt(exception=True).error(
                f"[excel_processor] task processing failed: id={record_id}, "
                f"file_path={getattr(record, 'file_path', None)}, "
                f"elapsed={time.monotonic() - task_started:.3f}s, "
                f"error_type={type(exc).__name__}, error={exc}"
            )
            # 当前事务可能已经在 flush/commit 阶段失败，必须先回滚再写失败状态。
            db.rollback()
            record = db.query(MeterExcelData).filter(MeterExcelData.id == record_id).first()
            if not record:
                logger.error(f"Excel processing failed and source record missing: id={record_id}, error={exc}")
                return True

            record.processing_status = "processing"
            record.processing_error = str(exc)
            record.processed_at = None
            db.commit()

            metrics = {"processing_failed": 1}
            rule_messages, rule_severity = evaluate_alarm_rules(db, "excel", metrics)
            fault_summary = merge_fault_summary(f"Excel解析失败: {exc}", rule_messages)
            upsert_search_index(db, "excel", record, record.location, True, fault_summary)
            fault_record = sync_fault_record(db, "excel", record, True, fault_summary, severity=rule_severity or "critical")
            _emit_fault_alarm(db, "excel", record, fault_summary, fault_record.id if fault_record else None)
            alarm_latency_ms = _complete_alarm_timing(record, True)
            record.processing_status = "failed"
            record.processed_at = datetime.now()
            db.commit()
            logger.warning(
                f"[excel_processor] failure alarm persisted: id={record.id}, "
                f"alarm_latency_ms={alarm_latency_ms:.3f}, target_met={alarm_latency_ms < 1000.0}"
            )
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
            logger.warning(
                f"[excel_processor] task marked failed: id={record.id}, "
                f"elapsed={time.monotonic() - task_started:.3f}s, "
                f"processing_error={record.processing_error}"
            )
        return True
    finally:
        db.close()


def process_next_excel_detail_backfill() -> bool:
    """把历史已解析但未拆明细的 Excel 记录回填到 meter_excel_measurement_details。"""
    db = SessionLocal()
    try:
        parse_result = db.query(MeterExcelParseResult).filter(
            MeterExcelParseResult.parsed_data_json.isnot(None),
            (MeterExcelParseResult.detail_summary_json.is_(None)) | (MeterExcelParseResult.detail_summary_json == "")
        ).order_by(MeterExcelParseResult.processed_at.asc()).first()
        if not parse_result:
            return False
        record = db.query(MeterExcelData).filter(MeterExcelData.id == parse_result.excel_id).first()
        if not record:
            parse_result.detail_summary_json = dumps_json({"row_count": 0, "metrics": {}, "sheets": {}, "error": "excel file missing"})
            db.commit()
            return True

        if record.file_path and Path(record.file_path).exists():
            # 历史数据优先重新解析原始 Excel，才能补齐本次新增的误差/% 和误差/ppm 明细。
            payload = parse_excel_file(record.file_path)
            parse_result.sheet_count = payload.get("sheet_count", parse_result.sheet_count or 0)
            parse_result.rated_voltage = payload.get("rated_voltage", parse_result.rated_voltage or 0)
            parse_result.rated_voltage_unit = payload.get("rated_voltage_unit", parse_result.rated_voltage_unit or "")
            parse_result.rated_frequency = payload.get("rated_frequency", parse_result.rated_frequency or 0)
            parse_result.rated_frequency_unit = payload.get("rated_frequency_unit", parse_result.rated_frequency_unit or "")
            parse_result.numeric_value_count = payload.get("numeric_value_count", parse_result.numeric_value_count or 0)
            parse_result.max_numeric_value = payload.get("max_numeric_value", parse_result.max_numeric_value or 0)
            parse_result.min_numeric_value = payload.get("min_numeric_value", parse_result.min_numeric_value or 0)
            parse_result.avg_numeric_value = payload.get("avg_numeric_value", parse_result.avg_numeric_value or 0)
            parse_result.parse_summary = dumps_json(payload.get("summary", {}))
            parse_result.parsed_data_json = dumps_json(_strip_excel_parsed_data_for_display(payload.get("parsed_data", {})))
        else:
            payload = {"parsed_data": _loads_json(parse_result.parsed_data_json) or {}}
        detail_rows, error_rows, detail_summary, detail_alarm_metrics = _build_excel_detail_rows_and_metrics(payload, record, parse_result.id)
        db.query(MeterExcelMeasurementDetail).filter(MeterExcelMeasurementDetail.excel_id == record.id).delete()
        db.query(MeterExcelErrorDetail).filter(MeterExcelErrorDetail.excel_id == record.id).delete()
        if detail_rows:
            db.bulk_save_objects(detail_rows)
        if error_rows:
            db.bulk_save_objects(error_rows)
        parse_result.chart_value_count = detail_summary.get("row_count", 0)
        parse_result.error_value_count = detail_summary.get("error_row_count", 0)
        parse_result.detail_summary_json = dumps_json(detail_summary)

        # 回填时也按当前规则跑一次细指标预警，保证历史数据能被新规则覆盖到。
        metrics = {
            "file_size_kb": round((record.file_size or 0) / 1024, 2),
            "sheet_count": parse_result.sheet_count or 0,
            "numeric_value_count": parse_result.numeric_value_count or 0,
            "chart_value_count": parse_result.chart_value_count or 0,
            "error_value_count": parse_result.error_value_count or 0,
            "max_numeric_value": parse_result.max_numeric_value or 0,
            "min_numeric_value": parse_result.min_numeric_value or 0,
            "avg_numeric_value": parse_result.avg_numeric_value or 0,
        }
        metrics.update(detail_alarm_metrics)
        base_fault, base_summary = detect_fault_flag(record.file_name, record.description)
        rule_messages, rule_severity = evaluate_alarm_rules(db, "excel", metrics)
        detected_fault = base_fault or bool(rule_messages)
        fault_summary = merge_fault_summary(base_summary, rule_messages)
        upsert_search_index(db, "excel", record, record.location, detected_fault, fault_summary)
        fault_record = sync_fault_record(db, "excel", record, detected_fault, fault_summary, severity=rule_severity)
        if detected_fault:
            _emit_fault_alarm(db, "excel", record, fault_summary, fault_record.id if fault_record else None)
        db.commit()
        logger.info(f"Excel detail backfilled: excel_id={record.id}, rows={detail_summary.get('row_count', 0)}")
        return True
    except Exception as exc:
        db.rollback()
        logger.opt(exception=True).error(
            f"[excel_processor] detail backfill failed: "
            f"error_type={type(exc).__name__}, error={exc}"
        )
        return True
    finally:
        db.close()


def process_next_image_record() -> bool:
    db = SessionLocal()
    try:
        record = db.query(MeterImageData).filter(
            MeterImageData.processing_status.in_(("pending", "processing"))
        ).order_by(MeterImageData.upload_time.asc()).first()
        if not record:
            return False

        record.processing_status = "processing"
        record.processing_error = None
        db.commit()
        db.refresh(record)
        record_id = record.id
        task_started = time.monotonic()
        _update_worker_state(
            "image",
            status="processing",
            current_task={
                "id": record.id,
                "device_id": record.device_id,
                "file_name": record.file_name,
                "file_path": record.file_path,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        logger.info(
            f"[image_processor] task claimed: id={record.id}, device_id={record.device_id}, "
            f"file_name={record.file_name}, file_path={record.file_path}, "
            f"file_size={record.file_size}, upload_time={record.upload_time}"
        )

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

            record.processing_status = "processing"
            record.processing_error = None
            record.processed_at = None
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
            alarm_latency_ms = _complete_alarm_timing(record, detected_fault)
            record.processing_status = "done"
            record.processed_at = datetime.now()
            db.commit()
            logger.info(
                f"[image_processor] alarm evaluation completed: id={record.id}, "
                f"alarm_triggered={detected_fault}, alarm_latency_ms={alarm_latency_ms:.3f}, "
                f"target_met={alarm_latency_ms < 1000.0}"
            )

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
            logger.info(
                f"[image_processor] task completed: id={record.id}, device_id={record.device_id}, "
                f"elapsed={time.monotonic() - task_started:.3f}s, "
                f"image_size={payload.get('image_width', 0)}x{payload.get('image_height', 0)}, "
                f"has_fault={payload.get('has_fault', False)}, "
                f"detected_fault={detected_fault}"
            )
        except Exception as exc:
            logger.opt(exception=True).error(
                f"[image_processor] task processing failed: id={record_id}, "
                f"file_path={getattr(record, 'file_path', None)}, "
                f"elapsed={time.monotonic() - task_started:.3f}s, "
                f"error_type={type(exc).__name__}, error={exc}"
            )
            # 当前事务可能已经在 flush/commit 阶段失败，必须先回滚再写失败状态。
            db.rollback()
            record = db.query(MeterImageData).filter(MeterImageData.id == record_id).first()
            if not record:
                logger.error(f"Image processing failed and source record missing: id={record_id}, error={exc}")
                return True

            record.processing_status = "processing"
            record.processing_error = str(exc)
            record.processed_at = None
            db.commit()

            metrics = {"processing_failed": 1}
            rule_messages, rule_severity = evaluate_alarm_rules(db, "image", metrics)
            fault_summary = merge_fault_summary(f"图片分析失败: {exc}", rule_messages)
            upsert_search_index(db, "image", record, record.location, True, fault_summary)
            fault_record = sync_fault_record(db, "image", record, True, fault_summary, severity=rule_severity or "critical")
            _emit_fault_alarm(db, "image", record, fault_summary, fault_record.id if fault_record else None)
            alarm_latency_ms = _complete_alarm_timing(record, True)
            record.processing_status = "failed"
            record.processed_at = datetime.now()
            db.commit()
            logger.warning(
                f"[image_processor] failure alarm persisted: id={record.id}, "
                f"alarm_latency_ms={alarm_latency_ms:.3f}, target_met={alarm_latency_ms < 1000.0}"
            )
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
            logger.warning(
                f"[image_processor] task marked failed: id={record.id}, "
                f"elapsed={time.monotonic() - task_started:.3f}s, "
                f"processing_error={record.processing_error}"
            )
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


def _strip_excel_parsed_data_for_display(parsed_data: dict) -> dict:
    """只保留上位机绘图需要的数据，明细预警字段已拆入独立表，避免 JSON 过大。"""
    display_data = {}
    for sheet_name, sheet_payload in (parsed_data or {}).items():
        clean_sheet = {
            "name": sheet_payload.get("name", sheet_name),
            "rated_voltage": sheet_payload.get("rated_voltage", 0),
            "rated_voltage_unit": sheet_payload.get("rated_voltage_unit", ""),
            "rated_frequency": sheet_payload.get("rated_frequency", 0),
            "rated_frequency_unit": sheet_payload.get("rated_frequency_unit", ""),
            "data": [],
        }
        for metric_block in sheet_payload.get("data", []) or []:
            metric_data = metric_block.get("data", {}) or {}
            x_payload = metric_data.get("x", {}) or {}
            clean_metric = {
                "name": metric_block.get("name", ""),
                "metric_key": metric_block.get("metric_key"),
                "value_unit": metric_block.get("value_unit", ""),
                "metric_group_index": metric_block.get("metric_group_index", 0),
                "data": {
                    "x": {
                        "name": list(x_payload.get("name", []) or []),
                        "data": list(x_payload.get("data", []) or []),
                    },
                    "y": metric_data.get("y", []) or [],
                },
            }
            clean_sheet["data"].append(clean_metric)
        display_data[str(sheet_name)] = clean_sheet
    return display_data

def _build_record_alarm_info(db: Session, data_type: str, file_id: int) -> dict:
    """读取数据文件对应的预警信息，供上位机数据查看页直接展示。"""
    empty_alarm = {
        "has_alarm": False,
        "fault_id": None,
        "severity": "",
        "status": "",
        "message": "",
        "created_at": None,
    }
    # 优先展示仍处于 open 的故障，避免最新一条 closed 记录把页面误显示成“无预警”。
    fault = db.query(FaultRecord).filter(
        FaultRecord.data_type == data_type,
        FaultRecord.file_id == file_id,
        FaultRecord.status != "closed",
    ).order_by(FaultRecord.created_at.desc()).first()
    if not fault:
        fault = db.query(FaultRecord).filter(
            FaultRecord.data_type == data_type,
            FaultRecord.file_id == file_id,
        ).order_by(FaultRecord.created_at.desc()).first()
    if fault and fault.status != "closed":
        return {
            "has_alarm": True,
            "fault_id": fault.id,
            "severity": fault.severity or "warning",
            "status": fault.status or "open",
            "message": fault.message or "",
            "created_at": fault.created_at.isoformat() if fault.created_at else None,
        }

    # 兜底读取搜索索引。上传时/解析时会维护 DataSearchIndex，旧数据即使缺 FaultRecord 也能在页面显示预警摘要。
    index = db.query(DataSearchIndex).filter(
        DataSearchIndex.data_type == data_type,
        DataSearchIndex.file_id == file_id,
        DataSearchIndex.has_fault == True,
    ).order_by(DataSearchIndex.occurred_at.desc()).first()
    if index:
        return {
            "has_alarm": True,
            "fault_id": fault.id if fault else None,
            "severity": (fault.severity if fault else None) or "warning",
            "status": (fault.status if fault else None) or "open",
            "message": index.fault_summary or "检测到预警",
            "created_at": (index.occurred_at or index.uploaded_at).isoformat() if (index.occurred_at or index.uploaded_at) else None,
        }
    return empty_alarm

def serialize_excel_record(db: Session, file_record: MeterExcelData, include_parsed_data: bool = False) -> dict:
    parse_result = db.query(MeterExcelParseResult).filter(MeterExcelParseResult.excel_id == file_record.id).first()
    summary = _loads_json(parse_result.parse_summary) if parse_result else None
    parsed_data = _loads_json(parse_result.parsed_data_json) if (include_parsed_data and parse_result) else None
    detail_summary = _loads_json(parse_result.detail_summary_json) if parse_result else None
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
        "alarm_evaluated": file_record.alarm_clock_finished_ns is not None,
        "alarm_latency_ms": file_record.alarm_latency_ms,
        "alarm_triggered": bool(file_record.alarm_triggered),
        "alarm_target_ms": 1000.0,
        "alarm_target_met": (
            file_record.alarm_latency_ms is not None and file_record.alarm_latency_ms < 1000.0
        ),
        "download_url": f"/api/file/download/excel/{file_record.id}",
        "alarm_info": _build_record_alarm_info(db, "excel", file_record.id),
        "parse_result": {
            "sheet_count": parse_result.sheet_count if parse_result else 0,
            "rated_voltage": parse_result.rated_voltage if parse_result else 0,
            "rated_voltage_unit": parse_result.rated_voltage_unit if parse_result else "",
            "rated_frequency": parse_result.rated_frequency if parse_result else 0,
            "rated_frequency_unit": parse_result.rated_frequency_unit if parse_result else "",
            "numeric_value_count": parse_result.numeric_value_count if parse_result else 0,
            "chart_point_count": parse_result.chart_point_count if parse_result else 0,
            "chart_value_count": parse_result.chart_value_count if parse_result else 0,
            "error_value_count": parse_result.error_value_count if parse_result else 0,
            "max_numeric_value": parse_result.max_numeric_value if parse_result else 0,
            "min_numeric_value": parse_result.min_numeric_value if parse_result else 0,
            "avg_numeric_value": parse_result.avg_numeric_value if parse_result else 0,
            "summary": summary or {},
            "detail_summary": detail_summary or {},
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
        "alarm_evaluated": file_record.alarm_clock_finished_ns is not None,
        "alarm_latency_ms": file_record.alarm_latency_ms,
        "alarm_triggered": bool(file_record.alarm_triggered),
        "alarm_target_ms": 1000.0,
        "alarm_target_met": (
            file_record.alarm_latency_ms is not None and file_record.alarm_latency_ms < 1000.0
        ),
        "download_url": f"/api/file/download/image/{file_record.id}",
        "alarm_info": _build_record_alarm_info(db, "image", file_record.id),
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





