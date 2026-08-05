"""Select eligible studies and cache bounded Chinese-summary enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from evidence_digest import config, eligibility, enrichment_store, store
from evidence_digest.cloudflare_ai import CloudflareAIClient, CloudflareAIError
from evidence_digest.config import PATHS, Paths
from evidence_digest.zh_summary import (
    MODEL_ID,
    PROMPT_VERSION,
    cache_matches,
    source_hash,
)


@dataclass(frozen=True)
class EnrichmentResult:
    eligible: int
    cached: int
    generated: int
    failed: int
    excluded: int


def run(
    *,
    window_days: int,
    limit: int = 200,
    paths: Paths = PATHS,
    today: date | None = None,
    client: CloudflareAIClient | None = None,
    records: list[dict] | None = None,
) -> EnrichmentResult:
    """Enrich the newest eligible cache misses, continuing after AI failures."""
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if client is None:
        raise ValueError("client is required")

    today = today or date.today()
    window_start = today - timedelta(days=window_days)
    archive_records = (
        records
        if records is not None
        else list(store.read_archive(window_days=window_days, today=today, paths=paths))
    )

    newest_by_pmid: dict[str, dict] = {}
    for record in archive_records:
        try:
            entry_date = date.fromisoformat(str(record.get("entryDate") or ""))
        except ValueError:
            continue
        if not window_start <= entry_date <= today:
            continue
        pmid = str(record.get("pmid") or "")
        current = newest_by_pmid.get(pmid)
        if current is None or (
            record.get("entryDate", ""), record.get("score", 0), pmid
        ) > (
            current.get("entryDate", ""), current.get("score", 0), pmid
        ):
            newest_by_pmid[pmid] = record

    allowed_journal_tas = {
        journal.ta for journal in config.load_journals(paths).journals
    }
    eligible_records = []
    excluded = 0
    for record in newest_by_pmid.values():
        if eligibility.assess(record, allowed_journal_tas).eligible:
            eligible_records.append(record)
        else:
            excluded += 1
    eligible_records.sort(
        key=lambda record: (
            record.get("entryDate", ""),
            record.get("score", 0),
            int(record["pmid"]),
        ),
        reverse=True,
    )

    latest_cache = enrichment_store.read_latest(paths)
    cached = 0
    misses = []
    for record in eligible_records:
        cached_record = latest_cache.get(str(record["pmid"]))
        if cached_record is not None and cache_matches(record, cached_record):
            cached += 1
        else:
            misses.append(record)

    successes = []
    failed = 0
    for record in misses[:limit]:
        try:
            summary = client.summarize(record)
        except CloudflareAIError as exc:
            failed += 1
            print(
                f"WARNING: Chinese enrichment failed for PMID {record['pmid']}: {exc}"
            )
            continue
        generated_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        successes.append(
            {
                "pmid": str(record["pmid"]),
                "sourceHash": source_hash(record),
                "model": MODEL_ID,
                "promptVersion": PROMPT_VERSION,
                "generatedAt": generated_at,
                **summary,
            }
        )

    if successes:
        enrichment_store.write_day(today, successes, paths)
    return EnrichmentResult(
        eligible=len(eligible_records),
        cached=cached,
        generated=len(successes),
        failed=failed,
        excluded=excluded,
    )
