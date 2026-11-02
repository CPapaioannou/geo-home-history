"""Validate Geo's quarter-hour electricity readings without HA dependencies."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from math import isfinite


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("History contains a non-numeric value")
    if not isfinite(value) or int(value) != value:
        raise ValueError("History contains an invalid integer")
    return int(value)


def complete_hours(payload: object, now: datetime) -> dict[str, int]:
    """Return UTC hour -> Wh. Never manufacture missing or incomplete hours.

    Duplicate pages are deduplicated; conflicting duplicates reject the response.
    Multiple tariff tiers are summed within a quarter, not counted as extra time.
    """
    if not isinstance(payload, list) or now.tzinfo is None:
        raise ValueError("Expected a list and a timezone-aware cutoff")
    intervals: dict[tuple[int, str], int] = {}
    for row in payload:
        if not isinstance(row, dict) or not isinstance(row.get("readings"), list):
            raise ValueError("Invalid history record")
        start = _integer(row.get("startTimestamp"))
        if start < 0 or start % 900:
            raise ValueError("History timestamp is not a quarter-hour UTC boundary")
        for reading in row["readings"]:
            if not isinstance(reading, dict):
                raise ValueError("Invalid reading")
            if reading.get("energyType") != "IMPORT":
                continue
            if _integer(reading.get("duration")) != 900:
                raise ValueError("Unsupported electricity interval duration")
            wh = _integer(reading.get("energyWattHours"))
            tier = reading.get("tierType")
            if wh < 0 or not isinstance(tier, str) or not tier:
                raise ValueError("Invalid electricity energy or tariff tier")
            key = (start, tier)
            if key in intervals and intervals[key] != wh:
                raise ValueError("Conflicting duplicate electricity interval")
            intervals[key] = wh
    quarters: dict[int, int] = defaultdict(int)
    for (start, _tier), wh in intervals.items():
        quarters[start] += wh
    hours: dict[int, dict[int, int]] = defaultdict(dict)
    for start, wh in quarters.items():
        hours[start // 3600 * 3600][start] = wh
    cutoff = int(now.timestamp()) // 3600 * 3600
    return {
        str(start): sum(parts.values())
        for start, parts in sorted(hours.items())
        if start < cutoff and set(parts) == {start + i * 900 for i in range(4)}
    }


def cumulative_rows(hours: dict[str, int], origin: datetime) -> list[dict]:
    """Recompute all sums so corrections propagate to every subsequent hour."""
    origin_ts = int(origin.timestamp())
    # Recorder computes the first delta from zero when no preceding sum exists.
    # A synthetic preceding hour would incorrectly present missing usage as zero.
    rows = []
    total = 0
    for key, wh in sorted(hours.items(), key=lambda item: int(item[0])):
        start = int(key)
        if start < origin_ts or start % 3600 or _integer(wh) < 0:
            raise ValueError("Invalid cached hour")
        total += wh
        rows.append({"start": datetime.fromtimestamp(start, UTC),
                     "state": wh / 1000, "sum": total / 1000})
    return rows
