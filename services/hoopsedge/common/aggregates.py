"""Adding up `AGG#` items. Shared by grading and the API, which must not load heavy libraries."""

from collections.abc import Iterable, Mapping
from typing import Any

COUNTS = ("legs", "won", "lost", "push", "void", "legs_won_on_lost", "legs_on_lost")
SUMS = ("profit", "breakeven_sum")


def merge_aggregates(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {
        **dict.fromkeys(COUNTS, 0),
        **dict.fromkeys(SUMS, 0.0),
        "buckets": {},
        "tickets": {"won": 0, "lost": 0, "pending": 0},
        "missed_by": {},
    }
    for item in items:
        for name in COUNTS:
            total[name] += int(item.get(name, 0))
        for name in SUMS:
            total[name] += float(item.get(name, 0.0))
        for key, bucket in item.get("buckets", {}).items():
            into = total["buckets"].setdefault(key, {"legs": 0, "won": 0, "p_sum": 0.0})
            into["legs"] += int(bucket["legs"])
            into["won"] += int(bucket["won"])
            into["p_sum"] += float(bucket.get("p_sum", 0.0))
        for key, count in item.get("tickets", {}).items():
            total["tickets"][key] = total["tickets"].get(key, 0) + int(count)
        for key, count in item.get("missed_by", {}).items():
            total["missed_by"][key] = total["missed_by"].get(key, 0) + int(count)
    return total
