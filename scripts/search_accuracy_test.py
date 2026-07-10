import argparse
import random
from datetime import datetime, timedelta
from typing import Dict, List


LOCATIONS = ["北京", "上海", "长沙", "苏州", "深圳"]


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
        rows.append({
            "data_type": data_type,
            "device_id": device_id,
            "location": location,
            "has_fault": has_fault,
            "occurred_at": occurred_at.isoformat(),
            "file_name": f"{device_id}_{data_type}_{idx}.dat",
        })
    return rows


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
    return True


def random_query(rng: random.Random) -> Dict:
    """Generate a mixed search query covering time, location, fault, and device filters."""
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


def run_api_false_positive_check(base_url: str, iterations: int, seed: int) -> float:
    """Check API search results for records that do not satisfy the submitted filters."""
    import json
    from urllib.parse import urlencode
    from urllib.request import urlopen

    rng = random.Random(seed)
    errors = 0
    checked = 0
    for _ in range(iterations):
        query = random_query(rng)
        search_url = base_url.rstrip("/") + "/api/search/data?" + urlencode(query)
        with urlopen(search_url, timeout=30) as response:
            dataset = json.loads(response.read().decode("utf-8")).get("dataset", [])
        for row in dataset:
            checked += 1
            if not matches(row, query):
                errors += 1
    return errors / max(1, checked)


def main():
    parser = argparse.ArgumentParser(description="Search accuracy regression test for NQI dataset retrieval.")
    parser.add_argument("--base-url", default=None, help="Optional server URL, for example http://localhost:8000")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--fixture-size", type=int, default=20000)
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


if __name__ == "__main__":
    main()
