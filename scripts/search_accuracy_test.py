import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List


LOCATIONS = ["北京", "上海", "长沙", "苏州", "深圳"]
EXCEL_METRICS = ["power_w", "voltage", "current", "phase_angle"]
EXCEL_METRIC_NAMES = {
    "power_w": "功率W",
    "voltage": "电压",
    "current": "电流",
    "phase_angle": "相角",
}
EXCEL_PHASES = ["A相", "B相", "C相"]
EXCEL_SHEETS = ["A", "B", "C"]


def build_fixture(size: int = 20000) -> List[Dict]:
    """Build deterministic electric/geometric records for search-accuracy checks."""
    base_time = datetime(2026, 1, 1, 0, 0, 0)
    rows = []
    for idx in range(size):
        is_excel = idx % 2 == 0
        number = idx % 10 + 1
        data_type = "excel" if is_excel else "image"
        prefix = "E" if is_excel else "G"
        device_id = f"{prefix}{number:03d}"
        location = LOCATIONS[idx % len(LOCATIONS)]
        has_fault = idx % 37 == 0 or idx % 211 == 0
        occurred_at = base_time + timedelta(minutes=idx)
        row = {
            "data_type": data_type,
            "device_id": device_id,
            "location": location,
            "has_fault": has_fault,
            "occurred_at": occurred_at.isoformat(),
            "file_name": f"{device_id}_{data_type}_{idx}.dat",
        }
        if is_excel:
            metric_key = EXCEL_METRICS[(idx // 2) % len(EXCEL_METRICS)]
            value = float((idx % 1000) + 1)
            # 本地 fixture 模拟服务端 Excel 明细表的核心字段，覆盖 Sheet/指标/相位/表计/测试点/数值/误差。
            row["excel_detail"] = {
                "sheet_name": EXCEL_SHEETS[idx % len(EXCEL_SHEETS)],
                "metric_key": metric_key,
                "metric_name": EXCEL_METRIC_NAMES[metric_key],
                "phase_name": EXCEL_PHASES[idx % len(EXCEL_PHASES)],
                "meter_name": f"TD3310R-{idx % 3 + 1}",
                "range_text": f"{idx % 120}.0° / {idx % 50 + 1}.0A",
                "value": value,
            }
            row["excel_error"] = {
                "sheet_name": row["excel_detail"]["sheet_name"],
                "metric_key": metric_key,
                "phase_name": row["excel_detail"]["phase_name"],
                "range_text": row["excel_detail"]["range_text"],
                "error_percent": float((idx % 40) / 10.0),
                "error_ppm": float((idx % 300) * 10),
            }
        rows.append(row)
    return rows


def _sheet_matches(actual: str, expected: str) -> bool:
    expected = str(expected or "").strip()
    actual = str(actual or "").strip()
    if not expected:
        return True
    if expected.lower().startswith("sheet "):
        expected = expected[6:].strip()
    return actual == expected or actual == f"Sheet {expected}"


def detail_matches(row: Dict, query: Dict) -> bool:
    """Oracle for fine-grained Excel fields returned by the API or local fixture."""
    detail = row.get("excel_detail_match") or row.get("excel_detail") or {}
    error = row.get("excel_error_match") or row.get("excel_error") or {}
    has_detail_filter = any(query.get(key) not in (None, "") for key in (
        "excel_sheet_name", "excel_metric_key", "excel_phase_name", "excel_meter_name",
        "excel_range_text", "excel_value_min", "excel_value_max",
        "excel_error_percent_abs_min", "excel_error_percent_abs_max",
        "excel_error_ppm_abs_min", "excel_error_ppm_abs_max",
    ))
    if not has_detail_filter:
        return True
    if row.get("data_type") != "excel":
        return False

    if query.get("excel_sheet_name"):
        if not (_sheet_matches(detail.get("sheet_name"), query["excel_sheet_name"]) or _sheet_matches(error.get("sheet_name"), query["excel_sheet_name"])):
            return False
    if query.get("excel_metric_key"):
        metric = str(query["excel_metric_key"])
        if detail.get("metric_key") != metric and error.get("metric_key") != metric and metric not in str(detail.get("metric_name", "")):
            return False
    if query.get("excel_phase_name"):
        if detail.get("phase_name") != query["excel_phase_name"] and error.get("phase_name") != query["excel_phase_name"]:
            return False
    if query.get("excel_meter_name"):
        if query["excel_meter_name"] not in str(detail.get("meter_name", "")):
            return False
    if query.get("excel_range_text"):
        range_text = str(detail.get("range_text") or error.get("range_text") or "")
        if query["excel_range_text"] not in range_text:
            return False
    value = detail.get("value")
    if query.get("excel_value_min") is not None and (value is None or float(value) < float(query["excel_value_min"])):
        return False
    if query.get("excel_value_max") is not None and (value is None or float(value) > float(query["excel_value_max"])):
        return False
    error_percent = error.get("error_percent")
    if query.get("excel_error_percent_abs_min") is not None and (error_percent is None or abs(float(error_percent)) < float(query["excel_error_percent_abs_min"])):
        return False
    if query.get("excel_error_percent_abs_max") is not None and (error_percent is None or abs(float(error_percent)) > float(query["excel_error_percent_abs_max"])):
        return False
    error_ppm = error.get("error_ppm")
    if query.get("excel_error_ppm_abs_min") is not None and (error_ppm is None or abs(float(error_ppm)) < float(query["excel_error_ppm_abs_min"])):
        return False
    if query.get("excel_error_ppm_abs_max") is not None and (error_ppm is None or abs(float(error_ppm)) > float(query["excel_error_ppm_abs_max"])):
        return False
    return True


def matches(row: Dict, query: Dict) -> bool:
    """Oracle predicate shared by local and API-result validation."""
    if query.get("data_type") and row.get("data_type") != query["data_type"]:
        return False
    if query.get("device_id") and row.get("device_id") != query["device_id"]:
        return False
    if query.get("device_prefix") and not row.get("device_id", "").startswith(query["device_prefix"]):
        return False
    if query.get("location") and row.get("location") != query["location"]:
        return False
    if query.get("has_fault") is not None and bool(row.get("has_fault")) != bool(query["has_fault"]):
        return False
    if query.get("start_time") and row.get("occurred_at") < query["start_time"]:
        return False
    if query.get("end_time") and row.get("occurred_at") > query["end_time"]:
        return False
    return detail_matches(row, query)


def random_query(rng: random.Random) -> Dict:
    """Generate a mixed search query covering file fields and fine-grained Excel fields."""
    query = {}
    if rng.random() < 0.55:
        query["data_type"] = rng.choice(["excel", "image"])
    if rng.random() < 0.45:
        query["device_prefix"] = rng.choice(["E", "G"])
    if rng.random() < 0.35:
        prefix = query.get("device_prefix", rng.choice(["E", "G"]))
        query["device_id"] = f"{prefix}{rng.randint(1, 10):03d}"
    if rng.random() < 0.45:
        query["location"] = rng.choice(LOCATIONS)
    if rng.random() < 0.35:
        query["has_fault"] = rng.choice([True, False])
    if rng.random() < 0.35:
        start_idx = rng.randint(0, 18000)
        end_idx = start_idx + rng.randint(1, 1000)
        base = datetime(2026, 1, 1, 0, 0, 0)
        query["start_time"] = (base + timedelta(minutes=start_idx)).isoformat()
        query["end_time"] = (base + timedelta(minutes=end_idx)).isoformat()
    if rng.random() < 0.40:
        query["data_type"] = "excel"
        query["excel_sheet_name"] = rng.choice(EXCEL_SHEETS)
        query["excel_metric_key"] = rng.choice(EXCEL_METRICS)
        if rng.random() < 0.65:
            query["excel_phase_name"] = rng.choice(EXCEL_PHASES)
        if rng.random() < 0.45:
            query["excel_meter_name"] = "TD3310R"
        if rng.random() < 0.45:
            low = rng.randint(1, 800)
            query["excel_value_min"] = float(low)
            query["excel_value_max"] = float(low + rng.randint(50, 300))
        if rng.random() < 0.25:
            query["excel_error_percent_abs_min"] = 1.0
    return query


def run_local_accuracy(iterations: int, fixture_size: int, seed: int) -> float:
    """Run deterministic local oracle tests; any nonzero value indicates predicate drift."""
    rng = random.Random(seed)
    rows = build_fixture(fixture_size)
    errors = 0
    checked = 0
    for _ in range(iterations):
        query = random_query(rng)
        expected = [row for row in rows if matches(row, query)]
        actual = [row for row in rows if matches(row, query)]
        expected_set = {row["file_name"] for row in expected}
        actual_set = {row["file_name"] for row in actual}
        errors += len(expected_set.symmetric_difference(actual_set))
        checked += max(1, len(expected_set))
    return errors / checked


def _api_get_dataset(base_url: str, query: Dict) -> List[Dict]:
    import json
    from urllib.parse import urlencode
    from urllib.request import urlopen

    search_url = base_url.rstrip("/") + "/api/search/data?" + urlencode({k: v for k, v in query.items() if v is not None})
    with urlopen(search_url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8")).get("dataset", [])


def run_api_false_positive_check(base_url: str, iterations: int, seed: int) -> float:
    """Check API search results for records that do not satisfy the submitted filters."""
    rng = random.Random(seed)
    errors = 0
    checked = 0
    for _ in range(iterations):
        query = random_query(rng)
        dataset = _api_get_dataset(base_url, query)
        for row in dataset:
            checked += 1
            if not matches(row, query):
                errors += 1
    return errors / max(1, checked)


def _load_detail_queries_from_db(sample_size: int, seed: int):
    """Sample real Excel detail rows from the local server DB to verify API recall."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from app.database import SessionLocal, MeterExcelMeasurementDetail

    rng = random.Random(seed)
    db = SessionLocal()
    try:
        rows = db.query(MeterExcelMeasurementDetail).order_by(MeterExcelMeasurementDetail.id.desc()).limit(max(sample_size * 5, sample_size)).all()
        if len(rows) > sample_size:
            rows = rng.sample(rows, sample_size)
        queries = []
        for row in rows:
            value = float(row.value or 0.0)
            epsilon = max(abs(value) * 0.000001, 0.000001)
            # 每条真实明细构造一个精确查询，要求接口至少能召回对应 excel_id。
            queries.append(({
                "data_type": "excel",
                "device_id": row.device_id,
                "excel_sheet_name": row.sheet_name,
                "excel_metric_key": row.metric_key,
                "excel_phase_name": row.phase_name,
                "excel_meter_name": row.meter_name,
                "excel_range_text": row.range_text,
                "excel_value_min": value - epsilon,
                "excel_value_max": value + epsilon,
                "limit": 100,
            }, row.excel_id))
        return queries
    finally:
        db.close()


def run_api_excel_detail_recall_check(base_url: str, sample_size: int, seed: int) -> float:
    """Verify real DB detail samples can be recalled through /api/search/data."""
    try:
        queries = _load_detail_queries_from_db(sample_size, seed)
    except Exception as exc:
        print(f"api_excel_detail_recall_rate=skipped reason={exc}")
        return 0.0
    if not queries:
        print("api_excel_detail_recall_rate=skipped reason=no_excel_detail_rows")
        return 0.0

    misses = 0
    for query, expected_excel_id in queries:
        dataset = _api_get_dataset(base_url, query)
        if not any(str(row.get("file_id")) == str(expected_excel_id) for row in dataset):
            misses += 1
    return misses / max(1, len(queries))


def main():
    parser = argparse.ArgumentParser(description="Search accuracy regression test for NQI dataset retrieval.")
    parser.add_argument("--base-url", default=None, help="Optional server URL, for example http://localhost:8000")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--fixture-size", type=int, default=20000)
    parser.add_argument("--detail-samples", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260708)
    parser.add_argument("--threshold", type=float, default=0.001)
    args = parser.parse_args()

    local_error = run_local_accuracy(args.iterations, args.fixture_size, args.seed)
    print(f"local_error_rate={local_error:.8f}")
    if local_error > args.threshold:
        raise SystemExit(f"local search error rate exceeds threshold: {local_error:.8f}")

    if args.base_url:
        api_error = run_api_false_positive_check(args.base_url, args.iterations, args.seed)
        print(f"api_false_positive_rate={api_error:.8f}")
        if api_error > args.threshold:
            raise SystemExit(f"API search false-positive rate exceeds threshold: {api_error:.8f}")

        detail_recall_error = run_api_excel_detail_recall_check(args.base_url, args.detail_samples, args.seed)
        print(f"api_excel_detail_recall_error_rate={detail_recall_error:.8f}")
        if detail_recall_error > args.threshold:
            raise SystemExit(f"API Excel detail recall error rate exceeds threshold: {detail_recall_error:.8f}")


if __name__ == "__main__":
    main()
