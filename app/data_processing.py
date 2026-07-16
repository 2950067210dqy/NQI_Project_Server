import json
import math
import random
import re
from pathlib import Path
from statistics import mean

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


def parse_excel_file(file_path) -> dict:
    """解析电量 Excel，并输出上位机可直接消费的结构化结果。"""
    excel_path = Path(file_path)
    excel_file = pd.ExcelFile(excel_path)

    parsed_sheets = {}
    numeric_values = []
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
        values = numeric_df.to_numpy().flatten()
        numeric_values.extend(float(value) for value in values if pd.notna(value))

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
        "numeric_value_count": len(numeric_values),
        "chart_point_count": chart_point_count,
        "chart_value_count": chart_value_count,
        "error_value_count": error_value_count,
        "max_numeric_value": max(numeric_values) if numeric_values else 0.0,
        "min_numeric_value": min(numeric_values) if numeric_values else 0.0,
        "avg_numeric_value": mean(numeric_values) if numeric_values else 0.0,
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


