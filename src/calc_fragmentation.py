#!/usr/bin/env python3
"""Fragmentation Index calculator for Apple Health step data.

Reads StepCount records from Apple Health XML export or a preprocessed CSV,
computes daily walking fragmentation metrics, and writes a CSV report.

Intentionally uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import csv
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Walk bout merging: records with gap <= this are merged into one bout
BOUT_MERGE_GAP_MINUTES = 5

# Long continuous bout threshold (minutes)
LONG_BOUT_THRESHOLD_MINUTES = 10

# Short fragment bout threshold (minutes)
SHORT_BOUT_THRESHOLD_MINUTES = 3

# Minimum daily steps to compute the index
MIN_DAILY_STEPS = 1000

# Anomaly filters
MAX_STEPS_PER_RECORD = 5000
MAX_RECORD_DURATION_FOR_MAX_STEPS = timedelta(minutes=5)
MAX_CADENCE = 200  # steps / minute
MIN_CADENCE = 40   # steps / minute

# Maximum number of bouts per day (cap for noise)
MAX_BOUTS_PER_DAY = 100

# Fragmentation index weights
WEIGHT_FRAGMENTATION_RATIO = 40
WEIGHT_BOUT_FREQUENCY = 30
WEIGHT_GAP_COEFFICIENT = 30

# Bout frequency baseline (20 bouts = 1.0 frequency score)
BOUT_FREQUENCY_BASELINE = 20

# Average bout duration baseline (15 min = 0 gap coefficient)
AVG_BOUT_BASELINE_MINUTES = 15

# Grade thresholds (inclusive lower bound)
GRADE_THRESHOLDS = [
    (81, "E"),
    (61, "D"),
    (41, "C"),
    (21, "B"),    (0, "A"),
]

# Apple Health date format
APPLE_DATE_FMT = "%Y-%m-%d %H:%M:%S %z"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class StepRecord:
    """A single step-count record."""
    __slots__ = ("start", "end", "value")

    def __init__(self, start: datetime, end: datetime, value: int):
        self.start = start
        self.end = end
        self.value = value

    @property
    def duration_min(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0

    @property
    def cadence(self) -> float:
        dur = self.duration_min
        return self.value / dur if dur > 0 else 0.0


class WalkBout:
    """A merged walking bout."""
    __slots__ = ("start", "end", "steps")

    def __init__(self, start: datetime, end: datetime, steps: int):
        self.start = start
        self.end = end
        self.steps = steps

    @property
    def duration_min(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0

    def merge(self, record: StepRecord) -> None:
        """Merge a step record into this bout."""
        if record.start < self.start:
            self.start = record.start
        if record.end > self.end:
            self.end = record.end
        self.steps += record.value


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def _parse_apple_datetime(s: str) -> datetime:
    """Parse an Apple Health date string.

    Apple Health uses "YYYY-MM-DD HH:MM:SS ±HHMM" format.
    """
    s = s.strip()
    try:
        return datetime.strptime(s, APPLE_DATE_FMT)
    except ValueError:
        # Fallback: try without timezone
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    # Assume local time - we only care about calendar day grouping
                    pass
                return dt
            except ValueError:
                continue
        raise ValueError(f"Unrecognized date format: {s!r}")


def read_records_from_xml(xml_path: Path) -> list[StepRecord]:
    """Read StepCount records from an Apple Health export.xml file.

    Uses iterparse for memory-efficient streaming of large files.
    """
    records: list[StepRecord] = []
    context = ET.iterparse(str(xml_path), events=("end",))
    for event, elem in context:
        if elem.tag == "Record" and elem.get("type") == "HKQuantityTypeIdentifierStepCount":
            try:
                start = _parse_apple_datetime(elem.get("startDate", ""))
                end = _parse_apple_datetime(elem.get("endDate", ""))
                value = int(float(elem.get("value", "0")))
                records.append(StepRecord(start, end, value))
            except (ValueError, TypeError):
                # Skip malformed records
                pass
        # Clear element to free memory
        elem.clear()
    return records


def read_records_from_csv(csv_path: Path) -> list[StepRecord]:
    """Read StepCount records from a CSV file.

    Expected columns (case-insensitive): startDate, endDate, value
    Also accepts: start, end, steps  as aliases.
    """
    records: list[StepRecord] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return records

        # Normalize column names (lowercase)
        field_map = {name.lower(): name for name in reader.fieldnames}

        # Detect column names
        def find_col(*candidates: str) -> str | None:
            for c in candidates:
                if c in field_map:
                    return field_map[c]
            return None

        start_col = find_col("startdate", "start", "start_time")
        end_col = find_col("enddate", "end", "end_time")
        value_col = find_col("value", "steps", "stepcount", "count")

        if not start_col or not end_col or not value_col:
            raise ValueError(
                f"CSV must contain startDate, endDate, value columns. "
                f"Found: {list(field_map.keys())}"
            )

        for row in reader:
            try:
                start = _parse_apple_datetime(row[start_col])
                end = _parse_apple_datetime(row[end_col])
                value = int(float(row[value_col]))
                records.append(StepRecord(start, end, value))
            except (ValueError, TypeError, KeyError):
                continue

    return records


def read_records(input_path: Path) -> list[StepRecord]:
    """Read step records from either XML or CSV, auto-detected by extension."""
    suffix = input_path.suffix.lower()
    if suffix == ".xml":
        return read_records_from_xml(input_path)
    elif suffix in (".csv", ".tsv"):
        return read_records_from_csv(input_path)
    else:
        # Try CSV first, then XML
        try:
            return read_records_from_csv(input_path)
        except Exception:
            return read_records_from_xml(input_path)


# ---------------------------------------------------------------------------
# Anomaly filtering
# ---------------------------------------------------------------------------

def is_anomalous(record: StepRecord) -> bool:
    """Return True if the record looks like bad data."""
    # Zero or negative steps
    if record.value <= 0:
        return True

    # Extremely high step count in short duration
    if record.value > MAX_STEPS_PER_RECORD and (record.end - record.start) <= MAX_RECORD_DURATION_FOR_MAX_STEPS:
        return True

    # Unrealistic cadence
    cadence = record.cadence
    if cadence > MAX_CADENCE or cadence < MIN_CADENCE:
        # Only flag if duration is meaningful (> 30 seconds)
        if record.duration_min >= 0.5:
            return True

    return False


def filter_records(records: list[StepRecord]) -> list[StepRecord]:
    """Filter out anomalous records."""
    return [r for r in records if not is_anomalous(r)]


# ---------------------------------------------------------------------------
# Walk bout detection
# ---------------------------------------------------------------------------

def merge_into_bouts(records: list[StepRecord]) -> list[WalkBout]:
    """Merge sorted step records into walking bouts.

    Records must be sorted by start time. Two consecutive records are merged
    if the gap between them is <= BOUT_MERGE_GAP_MINUTES.
    """
    if not records:
        return []

    bouts: list[WalkBout] = []
    current = WalkBout(records[0].start, records[0].end, records[0].value)

    for rec in records[1:]:
        gap = (rec.start - current.end).total_seconds() / 60.0
        if gap <= BOUT_MERGE_GAP_MINUTES:
            current.merge(rec)
        else:
            bouts.append(current)
            current = WalkBout(rec.start, rec.end, rec.value)

    bouts.append(current)

    # Cap at MAX_BOUTS_PER_DAY to guard against noise
    if len(bouts) > MAX_BOUTS_PER_DAY:
        # Keep the longest bouts (most relevant), drop the tiny fragments
        bouts.sort(key=lambda b: b.duration_min, reverse=True)
        bouts = bouts[:MAX_BOUTS_PER_DAY]
        bouts.sort(key=lambda b: b.start)

    return bouts


# ---------------------------------------------------------------------------
# Fragmentation index computation
# ---------------------------------------------------------------------------

def compute_fragmentation_index(bouts: list[WalkBout], total_steps: int) -> dict:
    """Compute fragmentation index and related metrics for one day.

    Returns a dict with all daily metrics.
    """
    total_bouts = len(bouts)

    if total_bouts == 0 or total_steps < MIN_DAILY_STEPS:
        return {
            "total_steps": total_steps,
            "walk_bouts_count": total_bouts,
            "long_bouts_count": 0,
            "short_bouts_count": 0,
            "avg_bout_min": 0.0,
            "fragmentation_index": None,
            "grade": "N/A",
            "status": "insufficient_data" if total_steps < MIN_DAILY_STEPS else "no_bouts",
        }

    long_bouts = sum(1 for b in bouts if b.duration_min >= LONG_BOUT_THRESHOLD_MINUTES)
    short_bouts = sum(1 for b in bouts if b.duration_min < SHORT_BOUT_THRESHOLD_MINUTES)

    total_walk_minutes = sum(b.duration_min for b in bouts)
    avg_bout_min = total_walk_minutes / total_bouts

    # Sub-metrics (each in 0~1 range, higher = more fragmented)
    fragmentation_ratio = short_bouts / max(total_bouts, 1)
    bout_frequency = min(total_bouts / BOUT_FREQUENCY_BASELINE, 1.0)
    gap_coefficient = 1.0 - min(avg_bout_min / AVG_BOUT_BASELINE_MINUTES, 1.0)

    # Composite index (0~100)
    index = (
        fragmentation_ratio * WEIGHT_FRAGMENTATION_RATIO
        + bout_frequency * WEIGHT_BOUT_FREQUENCY
        + gap_coefficient * WEIGHT_GAP_COEFFICIENT
    )
    index = round(max(0.0, min(100.0, index)), 1)

    # Grade
    grade = _grade_for_index(index)

    return {
        "total_steps": total_steps,
        "walk_bouts_count": total_bouts,
        "long_bouts_count": long_bouts,
        "short_bouts_count": short_bouts,
        "avg_bout_min": round(avg_bout_min, 1),
        "fragmentation_index": index,
        "grade": grade,
        "status": "ok",
    }


def _grade_for_index(index: float) -> str:
    """Map a fragmentation index to a letter grade."""
    for threshold, grade in GRADE_THRESHOLDS:
        if index >= threshold:
            return grade
    return "A"


# ---------------------------------------------------------------------------
# Daily aggregation
# ---------------------------------------------------------------------------

def group_records_by_date(records: list[StepRecord]) -> dict[date, list[StepRecord]]:
    """Group step records by calendar date (using record start date)."""
    by_date: dict[date, list[StepRecord]] = defaultdict(list)
    for rec in records:
        # Use the date of the start time (local time as encoded in the datetime)
        day = rec.start.date()
        by_date[day].append(rec)
    return by_date


def process_daily(records: list[StepRecord]) -> list[dict]:
    """Process all records and return per-day fragmentation metrics.

    Result rows are sorted by date ascending.
    """
    records = filter_records(records)
    by_date = group_records_by_date(records)

    results: list[dict] = []
    for day in sorted(by_date.keys()):
        day_records = sorted(by_date[day], key=lambda r: r.start)
        bouts = merge_into_bouts(day_records)
        total_steps = sum(r.value for r in day_records)
        metrics = compute_fragmentation_index(bouts, total_steps)
        metrics["date"] = day.isoformat()
        results.append(metrics)

    return results


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS = [
    "date",
    "total_steps",
    "walk_bouts_count",
    "long_bouts_count",
    "short_bouts_count",
    "avg_bout_min",
    "fragmentation_index",
    "grade",
]


def write_csv(results: list[dict], output_path: Path) -> None:
    """Write daily fragmentation metrics to a CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            # Handle None values
            out_row = {}
            for col in OUTPUT_COLUMNS:
                val = row.get(col, "")
                if val is None:
                    out_row[col] = ""
                else:
                    out_row[col] = val
            writer.writerow(out_row)


# ---------------------------------------------------------------------------
# Date range filtering
# ---------------------------------------------------------------------------

def filter_by_days(results: list[dict], days: int) -> list[dict]:
    """Keep only the most recent N days of results."""
    if days <= 0:
        return results
    if len(results) <= days:
        return results
    return results[-days:]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate daily walking fragmentation index from Apple Health data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --input export.xml --output fragmentation.csv
  %(prog)s --input step_records.csv --output fragmentation.csv --days 30
        """,
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to Apple Health export.xml or a CSV with step records.",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Path to output CSV file.",
    )
    parser.add_argument(
        "--days", "-d",
        type=int,
        default=0,
        help="Only process the most recent N days (0 = all days).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1

    # Read records
    print(f"Reading step records from {input_path} ...")
    try:
        records = read_records(input_path)
    except Exception as e:
        print(f"Error reading input: {e}", file=sys.stderr)
        return 1

    print(f"  Loaded {len(records)} step records")

    if not records:
        print("Warning: no step records found in input file.", file=sys.stderr)

    # Process
    results = process_daily(records)

    # Apply day limit
    if args.days > 0:
        results = filter_by_days(results, args.days)
        print(f"  Keeping most recent {args.days} days")

    print(f"  Computed metrics for {len(results)} days")

    # Stats summary
    valid = [r for r in results if r["fragmentation_index"] is not None]
    if valid:
        avg_idx = sum(r["fragmentation_index"] for r in valid) / len(valid)
        grade_counts: dict[str, int] = defaultdict(int)
        for r in valid:
            grade_counts[r["grade"]] += 1
        print(f"  Valid days: {len(valid)}, avg index: {avg_idx:.1f}")
        print(f"  Grade distribution: {dict(grade_counts)}")

    # Write output
    write_csv(results, output_path)
    print(f"Output written to {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
