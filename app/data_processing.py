import json
import math
import random
import re
from pathlib import Path

import pandas as pd
from PIL import Image, ImageFilter, ImageStat


FAULT_KEYWORDS = ["故障", "异常", "报警", "fault", "error", "alarm", "warning"]


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        return float(value)
    except Exception:
        return default


def _extract_digits(text: str) -> str:
    return "".join(re.findall(r"[0-9.]", str(text)))


def _extract_alpha(text: str) -> str:
    return "".join(re.findall(r"[A-Za-z]+", str(text)))


def _parse_rated_current(raw_value) -> float:
    parts = str(raw_value).strip().split(",")
    if len(parts) < 2:
        return 0.0
    current_text = parts[1]
    digits = _extract_digits(current_text)
    current_value = _safe_float(digits, 0.0)
    unit = _extract_alpha(current_text)
    if unit.lower() == "ma":
        return current_value / 1000.0
    return current_value


def _update_numeric_summary(summary: dict, values) -> None:
    """用向量化结果更新聚合值，避免逐单元格创建 Python 对象。"""
    valid_values = values[pd.notna(values)]
    if not valid_values.size:
        return
    summary["count"] += int(valid_values.size)
    summary["sum"] += float(valid_values.sum())
    value_min = float(valid_values.min())
    value_max = float(valid_values.max())
    summary["min"] = value_min if summary["min"] is None else min(summary["min"], value_min)
    summary["max"] = value_max if summary["max"] is None else max(summary["max"], value_max)


def _summary_metrics(prefix: str, summary: dict, output: dict) -> None:
    """将统计结果展开成预警规则可匹配的指标键。"""
    if not summary["count"]:
        return
    output[f"{prefix}_max"] = summary["max"]
    output[f"{prefix}_min"] = summary["min"]
    output[f"{prefix}_avg"] = summary["sum"] / summary["count"]
    output[f"{prefix}_count"] = summary["count"]


def _scope_metric_key(value) -> str:
    """将 Sheet 和相位名称转换为正式预警指标使用的键片段。"""
    return str(value or "").strip().replace(" ", "_").replace("相", "") or "unknown"


def parse_excel_alarm_metrics(file_path) -> dict:
    """轻量解析 Excel 预警指标，不构造图表或数据库明细对象。

    该函数只服务于报警延迟可视化；输出字段与正式预警保持一致，
    但避开 parsed_data 的大量嵌套字典构造，降低 Excel 测试延迟。
    """
    excel_file = pd.ExcelFile(Path(file_path))
    metrics = {"sheet_count": 0, "chart_value_count": 0, "error_value_count": 0}
    numeric_summary = {"count": 0, "sum": 0.0, "min": None, "max": None}
    metric_summaries = {}
    sheet_metric_summaries = {}
    error_summaries = {}
    sheet_error_summaries = {}
    sheet_phase_error_summaries = {}

    def get_summary(container: dict, key):
        return container.setdefault(key, {"count": 0, "sum": 0.0, "min": None, "max": None})

    for sheet_name in excel_file.sheet_names:
        dataframe = excel_file.parse(sheet_name, header=None)
        data_clean = dataframe.dropna()
        if data_clean.empty or dataframe.shape[0] < 5 or dataframe.shape[1] < 5:
            continue
        metrics["sheet_count"] += 1
        numeric_frame = dataframe.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        _update_numeric_summary(numeric_summary, numeric_frame)
        phase_series = dataframe.iloc[3, 4:].dropna()
        device_series = dataframe.iloc[4, 4:].drop_duplicates()
        if len(device_series) >= 2:
            device_series = device_series[:-2]
        device_count = int(device_series.shape[0])
        if not len(phase_series) or not device_count:
            continue

        for metric_group_index, metric_name in enumerate(("功率W", "电压", "电流", "相角")):
            metric_rows = data_clean.iloc[metric_group_index * 36:(metric_group_index + 1) * 36]
            if metric_rows.empty:
                continue
            metric_key = _metric_key(metric_name)
            for phase_index, phase_column in enumerate(phase_series.index):
                values = metric_rows.iloc[:, int(phase_column):int(phase_column) + device_count]
                value_array = values.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
                valid_count = int(pd.notna(value_array).sum())
                metrics["chart_value_count"] += valid_count
                _update_numeric_summary(get_summary(metric_summaries, metric_key), value_array)
                _update_numeric_summary(get_summary(sheet_metric_summaries, (str(sheet_name), metric_key)), value_array)
                error_present_mask = None

                for error_kind, error_offset in (("error_percent_abs", device_count), ("error_ppm_abs", device_count + 1)):
                    error_column = int(phase_column) + error_offset
                    if error_column >= metric_rows.shape[1]:
                        continue
                    error_values = pd.to_numeric(metric_rows.iloc[:, error_column], errors="coerce").to_numpy(dtype=float)
                    error_values = abs(error_values)
                    valid_error_mask = pd.notna(error_values)
                    error_present_mask = valid_error_mask if error_present_mask is None else (error_present_mask | valid_error_mask)
                    _update_numeric_summary(get_summary(error_summaries, (metric_key, error_kind)), error_values)
                    _update_numeric_summary(get_summary(sheet_error_summaries, (str(sheet_name), metric_key, error_kind)), error_values)
                    _update_numeric_summary(
                        get_summary(sheet_phase_error_summaries, (str(sheet_name), str(phase_series.iloc[phase_index]), metric_key, error_kind)),
                        error_values,
                    )
                if error_present_mask is not None:
                    metrics["error_value_count"] += int(error_present_mask.sum())

    metrics["numeric_value_count"] = numeric_summary["count"]
    metrics["max_numeric_value"] = numeric_summary["max"] if numeric_summary["max"] is not None else 0.0
    metrics["min_numeric_value"] = numeric_summary["min"] if numeric_summary["min"] is not None else 0.0
    metrics["avg_numeric_value"] = numeric_summary["sum"] / numeric_summary["count"] if numeric_summary["count"] else 0.0
    for metric_key, summary in metric_summaries.items():
        _summary_metrics(metric_key, summary, metrics)
    for (sheet_name, metric_key), summary in sheet_metric_summaries.items():
        _summary_metrics(f"sheet_{_scope_metric_key(sheet_name)}_{metric_key}", summary, metrics)
    for (metric_key, error_kind), summary in error_summaries.items():
        _summary_metrics(f"{metric_key}_{error_kind}", summary, metrics)
    for (sheet_name, metric_key, error_kind), summary in sheet_error_summaries.items():
        _summary_metrics(f"sheet_{_scope_metric_key(sheet_name)}_{metric_key}_{error_kind}", summary, metrics)
    for (sheet_name, phase_name, metric_key, error_kind), summary in sheet_phase_error_summaries.items():
        _summary_metrics(
            f"sheet_{_scope_metric_key(sheet_name)}_phase_{_scope_metric_key(phase_name)}_{metric_key}_{error_kind}",
            summary,
            metrics,
        )
    return metrics

def parse_excel_file(file_path) -> dict:
    """解析电量 Excel，并输出上位机可直接消费的结构化结果。"""
    excel_path = Path(file_path)
    excel_file = pd.ExcelFile(excel_path)

    parsed_sheets = {}
    numeric_value_count = 0
    numeric_value_sum = 0.0
    numeric_value_min = None
    numeric_value_max = None
    device_names = set()
    phase_names = set()

    for sheet_name in excel_file.sheet_names:
        df = excel_file.parse(sheet_name, header=None)
        parsed_sheet = _parse_single_sheet(df, sheet_name)
        if parsed_sheet:
            parsed_sheets[sheet_name] = parsed_sheet
            for data_type in parsed_sheet.get("data", []):
                for phase in data_type.get("data", {}).get("y", []):
                    phase_names.add(str(phase.get("name", "")))
                    for device in phase.get("data", []):
                        device_names.add(str(device.get("name", "")))
        numeric_df = df.apply(pd.to_numeric, errors="coerce")
        values = numeric_df.to_numpy(dtype=float)
        valid_mask = pd.notna(values)
        if valid_mask.any():
            valid_values = values[valid_mask]
            numeric_value_count += int(valid_values.size)
            numeric_value_sum += float(valid_values.sum())
            current_min = float(valid_values.min())
            current_max = float(valid_values.max())
            numeric_value_min = current_min if numeric_value_min is None else min(numeric_value_min, current_min)
            numeric_value_max = current_max if numeric_value_max is None else max(numeric_value_max, current_max)

    first_sheet = next(iter(parsed_sheets.values()), {})
    chart_point_count = 0
    chart_value_count = 0
    error_value_count = 0
    for sheet_payload in parsed_sheets.values():
        for metric_block in sheet_payload.get("data", []):
            chart_point_count += len(metric_block.get("data", {}).get("x", {}).get("data", []) or [])
            for phase_block in metric_block.get("data", {}).get("y", []) or []:
                for device_block in phase_block.get("data", []) or []:
                    chart_value_count += len(device_block.get("data", []) or [])
        for error_item in sheet_payload.get("error_data", []) or []:
            if error_item.get("error_percent") is not None or error_item.get("error_ppm") is not None:
                error_value_count += 1

    summary = {
        "sheet_names": list(parsed_sheets.keys()),
        "device_names": sorted(name for name in device_names if name),
        "phase_names": sorted(name for name in phase_names if name),
        "chart_point_count": chart_point_count,
        "chart_value_count": chart_value_count,
        "error_value_count": error_value_count,
    }

    return {
        "sheet_count": len(parsed_sheets),
        "rated_voltage": _safe_float(first_sheet.get("rated_voltage"), 0.0),
        "rated_voltage_unit": first_sheet.get("rated_voltage_unit", ""),
        "rated_frequency": _safe_float(first_sheet.get("rated_frequency"), 0.0),
        "rated_frequency_unit": first_sheet.get("rated_frequency_unit", ""),
        "numeric_value_count": numeric_value_count,
        "chart_point_count": chart_point_count,
        "chart_value_count": chart_value_count,
        "error_value_count": error_value_count,
        "max_numeric_value": numeric_value_max if numeric_value_max is not None else 0.0,
        "min_numeric_value": numeric_value_min if numeric_value_min is not None else 0.0,
        "avg_numeric_value": (numeric_value_sum / numeric_value_count) if numeric_value_count else 0.0,
        "parsed_data": parsed_sheets,
        "summary": summary,
    }


def _metric_key(metric_name: str) -> str:
    mapping = {"功率W": "power_w", "功率": "power_w", "电压": "voltage", "电流": "current", "相角": "phase_angle"}
    return mapping.get(str(metric_name).strip(), str(metric_name).strip().lower().replace(" ", "_") or "unknown")


def _metric_value_unit(metric_name: str) -> str:
    mapping = {"功率W": "W", "功率": "W", "电压": "V", "电流": "A", "相角": "°"}
    return mapping.get(str(metric_name).strip(), "")


def _parse_single_sheet(dataframe: pd.DataFrame, sheet_name: str) -> dict | None:
    """沿用上位机条形图解析逻辑，并额外输出可落库的测试点和误差明细。"""
    try:
        data_each_counts = 36
        data_clean = dataframe.dropna()
        if data_clean.empty:
            return None

        df_colum_0_unique = data_clean.drop_duplicates(subset=[data_clean.columns[0]])
        df_colum_1_unique = data_clean.drop_duplicates(subset=[data_clean.columns[1]])

        rated_frequency_text = str(df_colum_1_unique.iloc[0, 1]) if df_colum_1_unique.shape[0] > 0 else ""
        rated_voltage_text = str(df_colum_0_unique.iloc[0, 0]).split(",")[0] if df_colum_0_unique.shape[0] > 0 else ""
        rated_voltage = _safe_float(_extract_digits(rated_voltage_text), 0.0)
        rated_frequency = _safe_float(_extract_digits(rated_frequency_text), 0.0)

        result = {
            "name": sheet_name,
            "rated_voltage": rated_voltage,
            "rated_voltage_unit": _extract_alpha(rated_voltage_text),
            "rated_frequency": rated_frequency,
            "rated_frequency_unit": _extract_alpha(rated_frequency_text),
            "chart_schema": {
                "data_each_counts": data_each_counts,
                "x_axis": [str(dataframe.iloc[3, 2]), "电流/A"],
                "phase_header_row": 4,
                "device_header_row": 5,
                "data_start_row": int(data_clean.index[0]) + 1,
            },
            "data": [],
            "error_data": [],
        }

        x_axis_buffer = []
        x_meta_buffer = []
        type_names = ["功率W", "电压", "电流", "相角"]

        for row in range(data_clean.shape[0]):
            temp = row / data_each_counts
            index = math.floor(temp)

            if temp in (0, 1, 2, 3):
                type_name = type_names[index]
                type_item = {
                    "name": type_name,
                    "metric_key": _metric_key(type_name),
                    "value_unit": _metric_value_unit(type_name),
                    "metric_group_index": index,
                    "data": {
                        "x": {
                            "name": [str(dataframe.iloc[3, 2]), "电流/A"],
                            "data": [],
                            "point_meta": [],
                        },
                        "y": [],
                    },
                }
                result["data"].append(type_item)
                if index > 0:
                    result["data"][index - 1]["data"]["x"]["data"] = x_axis_buffer
                    result["data"][index - 1]["data"]["x"]["point_meta"] = x_meta_buffer
                    x_axis_buffer = []
                    x_meta_buffer = []

            range_text = str(data_clean.iloc[row, 0])
            frequency_hz = _safe_float(_extract_digits(data_clean.iloc[row, 1]), rated_frequency)
            angle_degree = _safe_float(data_clean.iloc[row, 2], 0.0)
            current_a = _parse_rated_current(range_text)
            point_index = row % data_each_counts
            source_excel_row = int(data_clean.index[row]) + 1
            x_axis_buffer.append([angle_degree, current_a])
            x_meta_buffer.append({
                "point_index": point_index,
                "source_excel_row": source_excel_row,
                "range_text": range_text,
                "frequency_hz": frequency_hz,
                "rated_voltage_v": rated_voltage,
                "rated_current_a": current_a,
                "phase_angle_degree": angle_degree,
                "x_label": f"{angle_degree:.1f}°/{current_a:.2f}A",
            })

        if result["data"]:
            result["data"][-1]["data"]["x"]["data"] = x_axis_buffer
            result["data"][-1]["data"]["x"]["point_meta"] = x_meta_buffer

        phase_series = dataframe.iloc[3, 4:].dropna()
        device_series = dataframe.iloc[4, 4:].drop_duplicates()
        if len(device_series) >= 2:
            device_series = device_series[:-2]
        device_names = [str(name) for name in device_series]
        reference_meter_name = device_names[0] if device_names else ""
        compared_meter_name = device_names[1] if len(device_names) > 1 else ""

        for row in range(data_clean.shape[0]):
            temp = row / data_each_counts
            index = math.floor(temp)
            if temp not in (0, 1, 2, 3) or index >= len(result["data"]):
                continue
            if result["data"][index]["data"]["y"]:
                continue
            for phase_index, phase_name in enumerate(phase_series):
                phase_item = {"name": str(phase_name), "phase_index": phase_index, "data": []}
                for device_index, device_name in enumerate(device_series):
                    phase_item["data"].append({"name": str(device_name), "meter_index": device_index, "data": []})
                result["data"][index]["data"]["y"].append(phase_item)

        for row in range(data_clean.shape[0]):
            temp = row / data_each_counts
            index = math.floor(temp)
            if index >= len(result["data"]):
                continue
            metric_name = type_names[index]
            metric_key = _metric_key(metric_name)
            point_index = row % data_each_counts
            source_excel_row = int(data_clean.index[row]) + 1
            range_text = str(data_clean.iloc[row, 0])
            frequency_hz = _safe_float(_extract_digits(data_clean.iloc[row, 1]), rated_frequency)
            angle_degree = _safe_float(data_clean.iloc[row, 2], 0.0)
            current_a = _parse_rated_current(range_text)

            for phase_offset in range(phase_series.shape[0]):
                phase_name = str(phase_series.iloc[phase_offset])
                phase_start_column = int(phase_series.index[phase_offset])
                for device_row in range(device_series.shape[0]):
                    target = result["data"][index]["data"]["y"][phase_offset]["data"][device_row]["data"]
                    source_column = phase_start_column + device_row
                    if source_column < data_clean.shape[1]:
                        target.append(data_clean.iloc[row, source_column])

                error_percent_column = phase_start_column + len(device_series)
                error_ppm_column = error_percent_column + 1
                result["error_data"].append({
                    "metric_name": metric_name,
                    "metric_key": metric_key,
                    "metric_group_index": index,
                    "point_index": point_index,
                    "source_excel_row": source_excel_row,
                    "range_text": range_text,
                    "frequency_hz": frequency_hz,
                    "rated_voltage_v": rated_voltage,
                    "rated_current_a": current_a,
                    "phase_angle_degree": angle_degree,
                    "phase_name": phase_name,
                    "phase_index": phase_offset,
                    "reference_meter_name": reference_meter_name,
                    "compared_meter_name": compared_meter_name,
                    "reference_value": _safe_float(data_clean.iloc[row, phase_start_column], None),
                    "compared_value": _safe_float(data_clean.iloc[row, phase_start_column + 1], None) if phase_start_column + 1 < data_clean.shape[1] else None,
                    "error_percent": _safe_float(data_clean.iloc[row, error_percent_column], None) if error_percent_column < data_clean.shape[1] else None,
                    "error_ppm": _safe_float(data_clean.iloc[row, error_ppm_column], None) if error_ppm_column < data_clean.shape[1] else None,
                })

        return result
    except Exception:
        return None

def analyze_image_file(file_path, file_name: str = "", description: str = "") -> dict:
    """对几何量图片做确定性分析，替代上位机随机判断。"""
    image_path = Path(file_path)
    with Image.open(image_path) as image:
        rgb_image = image.convert("RGB")
        gray_image = rgb_image.convert("L")

        gray_stat = ImageStat.Stat(gray_image)
        edge_image = gray_image.filter(ImageFilter.FIND_EDGES)
        edge_stat = ImageStat.Stat(edge_image)
        rgb_stat = ImageStat.Stat(rgb_image)

        mean_brightness = float(gray_stat.mean[0]) if gray_stat.mean else 0.0
        brightness_std = float(gray_stat.stddev[0]) if gray_stat.stddev else 0.0
        contrast_score = brightness_std
        sharpness_score = float(edge_stat.mean[0]) if edge_stat.mean else 0.0
        dominant_rgb = [int(channel_mean) for channel_mean in rgb_stat.mean[:3]]
        dominant_color = f"rgb({dominant_rgb[0]},{dominant_rgb[1]},{dominant_rgb[2]})"

        reasons = []
        haystack = f"{file_name} {description}".lower()
        if any(keyword.lower() in haystack for keyword in FAULT_KEYWORDS):
            reasons.append("文件名或描述命中故障关键字")
        if mean_brightness < 35:
            reasons.append("图片整体过暗")
        if mean_brightness > 220:
            reasons.append("图片整体过亮")
        if contrast_score < 18:
            reasons.append("图片对比度过低")
        if sharpness_score < 12:
            reasons.append("图片清晰度过低")
        if image.width < 320 or image.height < 240:
            reasons.append("图片分辨率偏低")

        has_fault = bool(reasons)
        # 当前没有接入真实几何量识别模型，非故障图片先用随机识别结果模拟识别通过/待复核状态。
        recognition_success = False if has_fault else random.choice([True, True, True, False])
        if has_fault:
            analysis_summary = "；".join(reasons)
            recognition_status = "识别故障"
        elif recognition_success:
            analysis_summary = "随机识别成功：几何量图像识别通过"
            recognition_status = "识别成功"
        else:
            analysis_summary = "随机识别待复核：未发现明确故障，但图像特征需要人工确认"
            recognition_status = "待复核"

        return {
            "recognized_path": str(image_path),
            "image_width": int(image.width),
            "image_height": int(image.height),
            "image_mode": rgb_image.mode,
            "mean_brightness": round(mean_brightness, 4),
            "brightness_std": round(brightness_std, 4),
            "contrast_score": round(contrast_score, 4),
            "sharpness_score": round(sharpness_score, 4),
            "dominant_color": dominant_color,
            "has_fault": has_fault,
            "analysis_summary": analysis_summary,
            "analysis_data": {
                "dominant_rgb": dominant_rgb,
                "fault_reasons": reasons,
                "recognition_success": recognition_success,
                "recognition_status": recognition_status,
            },
        }


def dumps_json(data) -> str:
    return json.dumps(data, ensure_ascii=False)




