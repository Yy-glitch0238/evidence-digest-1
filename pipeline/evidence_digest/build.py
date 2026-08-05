"""Turn the committed archive into the static API tree GitHub Pages serves.

Runs entirely offline against `data/archive/*.jsonl.gz` - no network, and no
re-classification or re-scoring (that already happened once, at harvest time,
and archived records are treated as final). This module's only job is
selection, sorting, capping, and reshaping into the exact files
contract/api.schema.json describes.

Every cap and window here comes from pipeline/config/scoring.json (`limits`),
except the highlights lookback, which the product spec fixes at 7 days
regardless of how deep the served window is - "what matters this week" is a
narrower, fixed-width lens than "what's in the archive".
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from collections import defaultdict
from pathlib import Path

from evidence_digest import eligibility, enrichment_store, zh_summary
from evidence_digest.config import PATHS, Journal, Paths, ScoringConfig, Taxonomy, load_all
from evidence_digest.feeds import write_all_feeds, write_chinese_feed

# Bumped only when the Study shape itself changes in a breaking way. Also used
# as api/taxonomy.json's `version`, since that file's shape tracks the same
# contract.
DATA_VERSION = 1

# Highlights are "what mattered this week" - a fixed lens, not a tunable knob.
HIGHLIGHTS_WINDOW_DAYS = 7

CARD_STRIPPED_KEYS = ("abstract", "sections", "mesh", "keywords")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lean_card(study: dict) -> dict:
    return {k: v for k, v in study.items() if k not in CARD_STRIPPED_KEYS}


def _sort_key(study: dict) -> tuple[int, int, int]:
    """Newest entryDate first, then score descending, then pmid ascending -
    a total order so rebuilds from the same archive are byte-identical."""
    entry_ord = dt.date.fromisoformat(study["entryDate"]).toordinal()
    return (-entry_ord, -int(study["score"]), int(study["pmid"]))


def _write_json(path: Path, data: object, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    else:
        text = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")


def _pubmed_journal_url(ta: str) -> str:
    term = f'"{ta}"[ta]'
    return "https://pubmed.ncbi.nlm.nih.gov/?term=" + urllib.parse.quote(term)


def _public_taxonomy(taxonomy: Taxonomy) -> dict:
    return {
        "version": DATA_VERSION,
        "specialties": [
            {
                "slug": s.slug,
                "name": s.name,
                "icon": s.icon,
                "blurb": s.blurb,
                "topics": [
                    {"slug": t.slug, "name": t.name, "blurb": t.blurb} for t in s.topics
                ],
            }
            for s in taxonomy.specialties
        ],
    }


def _public_journals(journals: list[Journal], generated_at: str) -> dict:
    return {
        "generatedAt": generated_at,
        "count": len(journals),
        "journals": [
            {
                "name": j.name,
                "ta": j.ta,
                "specialty": j.specialty,
                "tier": j.tier,
                "scope": j.scope,
                "pubmedUrl": _pubmed_journal_url(j.ta),
            }
            for j in journals
        ],
    }


def _select_highlights(
    records_last_7_days: list[dict], scoring: ScoringConfig
) -> list[dict]:
    highlights_cap = scoring.limits["highlights"]
    per_specialty_cap = scoring.limits["highlightsPerSpecialty"]

    # "Top scorers" - score drives order here, with the usual entryDate/pmid
    # tiebreak only to keep output deterministic when scores are equal.
    ordered = sorted(
        records_last_7_days,
        key=lambda s: (-int(s["score"]), -dt.date.fromisoformat(s["entryDate"]).toordinal(), int(s["pmid"])),
    )

    per_specialty_taken: dict[str, int] = defaultdict(int)
    included: set[str] = set()
    highlights: list[dict] = []
    for study in ordered:
        if study["pmid"] in included:
            continue
        if not any(per_specialty_taken[sp] < per_specialty_cap for sp in study["specialties"]):
            continue
        for sp in study["specialties"]:
            per_specialty_taken[sp] += 1
        included.add(study["pmid"])
        highlights.append(study)
        if len(highlights) >= highlights_cap:
            break
    return highlights


def build(
    window_days: int | None = None,
    site_url: str = "",
    paths: Paths = PATHS,
    today: dt.date | None = None,
    records: list[dict] | None = None,
    zh_summaries: dict[str, dict] | None = None,
) -> dict:
    """Rebuild the whole data/api + data/feeds tree from the archive.

    `records` lets tests (and nothing else) inject a synthetic archive result
    instead of reading data/archive from disk. `zh_summaries` optionally injects
    the enrichment cache while retaining the normal on-disk behavior by default.
    """
    from evidence_digest import store  # local import: keeps build.py testable without touching disk unless asked

    journals_cfg, taxonomy, scoring = load_all(paths)
    today = today or dt.date.today()
    window_days = window_days if window_days is not None else scoring.limits["servedWindowDays"]
    generated_at = _now_iso()

    all_records = list(records) if records is not None else list(store.read_archive(window_days, today, paths))

    studies_by_topic: dict[str, list[dict]] = defaultdict(list)
    studies_by_specialty: dict[str, list[dict]] = defaultdict(list)
    studies_by_day: dict[str, list[dict]] = defaultdict(list)

    for study in all_records:
        for topic_slug in study["topics"]:
            studies_by_topic[topic_slug].append(study)
        for specialty_slug in study["specialties"]:
            studies_by_specialty[specialty_slug].append(study)
        studies_by_day[study["entryDate"]].append(study)

    # ---- api/topics/<slug>.json --------------------------------------- #
    per_topic_cap = scoring.limits["perTopicFile"]
    topic_counts: dict[str, int] = {}
    for topic in taxonomy.all_topics:
        studies = studies_by_topic.get(topic.slug, [])
        ordered = sorted(studies, key=_sort_key)
        total = len(ordered)
        topic_counts[topic.slug] = total
        returned = min(total, per_topic_cap)
        cards = [_lean_card(s) for s in ordered[:returned]]
        _write_json(
            paths.api_dir / "topics" / f"{topic.slug}.json",
            {
                "topic": topic.slug,
                "specialty": topic.specialty_slug,
                "generatedAt": generated_at,
                "total": total,
                "returned": returned,
                "studies": cards,
            },
        )

    specialty_counts = {slug: len(studies) for slug, studies in studies_by_specialty.items()}
    for specialty in taxonomy.specialties:
        specialty_counts.setdefault(specialty.slug, 0)

    # ---- api/days/<date>.json ------------------------------------------ #
    days_sorted = sorted(studies_by_day, reverse=True)
    for date_str in days_sorted:
        ordered = sorted(studies_by_day[date_str], key=_sort_key)
        _write_json(
            paths.api_dir / "days" / f"{date_str}.json",
            {
                "date": date_str,
                "generatedAt": generated_at,
                "total": len(ordered),
                "studies": ordered,
            },
        )

    # ---- api/highlights.json -------------------------------------------- #
    since = (today - dt.timedelta(days=HIGHLIGHTS_WINDOW_DAYS)).isoformat()
    last_7_days_records = [s for s in all_records if s["entryDate"] >= since]
    highlights = _select_highlights(last_7_days_records, scoring)
    _write_json(
        paths.api_dir / "highlights.json",
        {
            "generatedAt": generated_at,
            "since": since,
            "studies": [_lean_card(s) for s in highlights],
        },
    )

    # ---- api/search-index.json ------------------------------------------ #
    min_score = scoring.limits["searchIndexMinScore"]
    index_window = scoring.limits["searchIndexWindowDays"]
    index_since = (today - dt.timedelta(days=index_window)).isoformat()
    index_candidates = [
        s for s in all_records if s["entryDate"] >= index_since and int(s["score"]) >= min_score
    ]
    index_candidates.sort(key=_sort_key)
    _write_json(
        paths.api_dir / "search-index.json",
        {
            "generatedAt": generated_at,
            "minScore": min_score,
            "windowDays": index_window,
            "entries": [
                {
                    "p": s["pmid"],
                    "t": s["title"],
                    "j": s["journal"]["name"],
                    "d": s["entryDate"],
                    "s": s["score"],
                    "tp": s["topics"],
                }
                for s in index_candidates
            ],
        },
    )

    # ---- api/taxonomy.json, api/journals.json ---------------------------- #
    _write_json(paths.api_dir / "taxonomy.json", _public_taxonomy(taxonomy))
    _write_json(paths.api_dir / "journals.json", _public_journals(list(journals_cfg.journals), generated_at))

    # ---- api/manifest.json ------------------------------------------------ #
    manifest = {
        "dataVersion": DATA_VERSION,
        "generatedAt": generated_at,
        "windowDays": window_days,
        "days": days_sorted,
        "latestDay": days_sorted[0] if days_sorted else today.isoformat(),
        "totalStudies": len(all_records),
        "journalCount": len(journals_cfg.journals),
        "topicCounts": topic_counts,
        "specialtyCounts": specialty_counts,
    }
    _write_json(paths.api_dir / "manifest.json", manifest, pretty=True)

    # ---- .nojekyll ---------------------------------------------------- #
    paths.api_dir.mkdir(parents=True, exist_ok=True)
    (paths.api_dir / ".nojekyll").write_text("", encoding="utf-8")

    # ---- feeds ----------------------------------------------------------- #
    feed_items_cap = scoring.limits["feedItems"]
    feed_count = write_all_feeds(
        taxonomy=taxonomy,
        studies_by_topic={
            slug: [_lean_card(s) for s in sorted(studies, key=_sort_key)[:feed_items_cap]]
            for slug, studies in studies_by_topic.items()
        },
        all_studies=[_lean_card(s) for s in sorted(all_records, key=_sort_key)[:feed_items_cap]],
        site_url=site_url,
        feeds_dir=paths.feeds_dir,
        generated_at=generated_at,
    )

    summaries = enrichment_store.read_latest(paths) if zh_summaries is None else zh_summaries
    allowed_journal_tas = {journal.ta for journal in journals_cfg.journals}
    zh_candidates = [
        study
        for study in all_records
        if eligibility.assess(study, allowed_journal_tas).eligible
        and study["pmid"] in summaries
        and zh_summary.cache_matches(study, summaries[study["pmid"]])
    ]
    zh_candidates.sort(key=_sort_key)
    zh_records: list[dict] = []
    seen_zh_pmids: set[str] = set()
    for study in zh_candidates:
        if study["pmid"] in seen_zh_pmids:
            continue
        seen_zh_pmids.add(study["pmid"])
        zh_records.append(study)
        if len(zh_records) == 200:
            break

    write_chinese_feed(
        paths.feeds_dir / "selected-journals-zh.xml",
        studies=zh_records,
        summaries=summaries,
        site_url=site_url,
        generated_at=generated_at,
    )
    feed_count += 1

    return {
        "totalStudies": len(all_records),
        "topics": len(taxonomy.all_topics),
        "days": len(days_sorted),
        "journals": len(journals_cfg.journals),
        "highlights": len(highlights),
        "searchIndexEntries": len(index_candidates),
        "feeds": feed_count,
        "windowDays": window_days,
        "generatedAt": generated_at,
    }
