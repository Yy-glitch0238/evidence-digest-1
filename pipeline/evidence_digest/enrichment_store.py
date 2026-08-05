"""Dated, deterministic cache storage for validated Chinese summaries."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

from evidence_digest.config import PATHS, Paths
from evidence_digest.store import _write_jsonl_gz
from evidence_digest.zh_summary import validate_cache_record


def cache_dir(paths: Paths = PATHS) -> Path:
    return paths.data_dir / "enrichment" / "zh"


def cache_path(day: dt.date, paths: Paths = PATHS) -> Path:
    return cache_dir(paths) / f"{day.isoformat()}.jsonl.gz"


def _read_day(path: Path) -> list[dict]:
    if not path.exists():
        return []

    records = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if validate_cache_record(record):
                records.append(record)
    return records


def _cache_dates(paths: Paths = PATHS) -> list[dt.date]:
    directory = cache_dir(paths)
    if not directory.is_dir():
        return []

    dates = []
    for path in directory.glob("*.jsonl.gz"):
        date_text = path.name[: -len(".jsonl.gz")]
        try:
            dates.append(dt.date.fromisoformat(date_text))
        except ValueError:
            continue
    return sorted(dates)


def read_latest(paths: Paths = PATHS) -> dict[str, dict]:
    """Return the latest valid cache record for every PMID across dated files."""
    latest: dict[str, dict] = {}
    for day in _cache_dates(paths):
        for record in _read_day(cache_path(day, paths)):
            latest[record["pmid"]] = record
    return latest


def write_day(day: dt.date, records: list[dict], paths: Paths = PATHS) -> int:
    """Merge valid summaries into a day file and return its valid record count."""
    path = cache_path(day, paths)
    merged = {record["pmid"]: record for record in _read_day(path)}
    for record in records:
        if validate_cache_record(record):
            merged[record["pmid"]] = record

    written = sorted(merged.values(), key=lambda record: (int(record["pmid"]), record["sourceHash"]))
    _write_jsonl_gz(path, written)
    return len(written)
