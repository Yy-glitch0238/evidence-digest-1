"""`python -m evidence_digest.cli <command>` - the only entry point a human or
a GitHub Actions workflow needs.

This module is the wiring: it is the one place that knows how parse.py,
classify.py, score.py, and takeaway.py compose into a single
contract/study.schema.json-shaped record, and how that record flows into
store.py. Every other module stays independently testable and, for parse/
classify/score/takeaway, importable with zero network access.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from evidence_digest import build as build_mod
from evidence_digest import classify as classify_mod
from evidence_digest import config
from evidence_digest import enrich as enrich_mod
from evidence_digest import parse as parse_mod
from evidence_digest import pubmed
from evidence_digest import score as score_mod
from evidence_digest import store
from evidence_digest import takeaway as takeaway_mod
from evidence_digest.cloudflare_ai import CloudflareAIClient
from evidence_digest.config import ConfigError, Journal

DEFAULT_MAX_PER_JOURNAL = 500
EFETCH_BATCH_SIZE = 200

# Backfill dedups against every PMID ever seen up to the target day, not a
# rolling window - the whole point is reconstructing history from scratch.
BACKFILL_SEEN_LOOKBACK_DAYS = 3650


# --------------------------------------------------------------------------- #
# assembling a contract-shaped Study from a parsed record
# --------------------------------------------------------------------------- #


def _assemble_study(
    parsed: dict,
    journal: Journal,
    classifier: classify_mod.Classifier,
    scoring: config.ScoringConfig,
    today: dt.date,
) -> dict:
    entry_date = parsed.get("entryDate") or ""
    if not entry_date:
        # Extremely rare (parse.py's own fallback chain almost always
        # produces something), but the contract requires entryDate always be
        # a valid YYYY-MM-DD - falling back to the harvest run date beats
        # emitting an invalid record.
        entry_date = today.isoformat()
    if parsed.get("entryDate") != entry_date:
        parsed = {**parsed, "entryDate": entry_date}

    topics, specialties = classifier.classify(parsed, journal)
    evidence = score_mod.detect_evidence(parsed, scoring)
    score_value = score_mod.score_study(parsed, journal, scoring, today, evidence=evidence)
    takeaway_text = takeaway_mod.build_takeaway(parsed)

    pmid = parsed["pmid"]
    doi = parsed.get("doi")
    pmcid = parsed.get("pmcid")
    abstract = parsed.get("abstract") or ""

    return {
        "pmid": pmid,
        "doi": doi,
        "title": parsed.get("title") or "",
        "abstract": abstract,
        "sections": parsed.get("sections") or {},
        "takeaway": takeaway_text,
        "authors": parsed.get("authors") or [],
        "authorLine": parsed.get("authorLine") or "",
        "journal": {"name": journal.name, "ta": journal.ta, "tier": journal.tier},
        "specialties": specialties,
        "topics": topics,
        "pubTypes": parsed.get("pubTypes") or [],
        "evidence": {"level": evidence.level, "label": evidence.label, "rank": evidence.rank},
        "score": score_value,
        "pubdate": parsed.get("pubdate") or "",
        "entryDate": entry_date,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "doiUrl": f"https://doi.org/{doi}" if doi else None,
        "pmcid": pmcid,
        "openAccess": bool(pmcid),
        "mesh": parsed.get("mesh") or [],
        "keywords": parsed.get("keywords") or [],
        "trialIds": parsed.get("trialIds") or [],
        "hasAbstract": bool(abstract),
    }


def _build_term(journal: Journal, topic_filter: str) -> str:
    clause = f'"{journal.ta}"[ta]'
    if journal.scope == "topic":
        clause = f"({clause}) AND {topic_filter}"
    return clause


# --------------------------------------------------------------------------- #
# shared harvest core (used by both `harvest` and `backfill`)
# --------------------------------------------------------------------------- #


@dataclass
class JournalStat:
    name: str
    ta: str
    found: int
    total_count: int
    new: int
    truncated: bool
    error: str | None


def _run_harvest(
    journals_cfg: config.JournalsConfig,
    classifier: classify_mod.Classifier,
    scoring: config.ScoringConfig,
    *,
    seen: set[str],
    today: dt.date,
    max_per_journal: int,
    only_journal: str | None,
    esearch_fn,
) -> tuple[list[dict], list[JournalStat], list[str]]:
    """esearch_fn(term, retmax) -> (ids, count, translation). Encapsulating the
    esearch call this way lets `harvest` use a rolling reldate window and
    `backfill` use an absolute mindate/maxdate day without duplicating the
    per-journal / dedup / efetch / assemble logic below."""
    journals_to_query = list(journals_cfg.journals)
    if only_journal is not None:
        journals_to_query = [j for j in journals_to_query if j.ta == only_journal]
        if not journals_to_query:
            raise SystemExit(f"--only-journal: no journal with ta == {only_journal!r} in journals.json")

    candidate_journal: dict[str, Journal] = {}
    stats: list[JournalStat] = []
    for journal in journals_to_query:
        term = _build_term(journal, journals_cfg.topic_filter)
        try:
            ids, count, _translation = esearch_fn(term, max_per_journal)
        except pubmed.PubMedError as exc:
            stats.append(JournalStat(journal.name, journal.ta, 0, 0, 0, False, str(exc)))
            continue

        truncated = count > max_per_journal
        new_here = 0
        for pid in ids:
            if pid in seen:
                continue
            if pid not in candidate_journal:
                candidate_journal[pid] = journal
                new_here += 1
        stats.append(JournalStat(journal.name, journal.ta, len(ids), count, new_here, truncated, None))

    new_pmids = list(candidate_journal)

    parsed_by_pmid: dict[str, dict] = {}
    fetch_errors: list[str] = []
    for i in range(0, len(new_pmids), EFETCH_BATCH_SIZE):
        batch = new_pmids[i : i + EFETCH_BATCH_SIZE]
        try:
            xml_bytes = pubmed.efetch(batch)
            parsed_by_pmid.update(parse_mod.parse_articles(xml_bytes))
        except pubmed.PubMedError as exc:
            fetch_errors.append(f"efetch batch starting pmid={batch[0]}: {exc}")
        except ET.ParseError as exc:
            fetch_errors.append(f"efetch batch starting pmid={batch[0]}: XML parse error: {exc}")

    studies: list[dict] = []
    for pid in new_pmids:
        parsed = parsed_by_pmid.get(pid)
        if parsed is None:
            continue  # efetch never returned this pmid; it stays unseen and will be retried next run
        studies.append(_assemble_study(parsed, candidate_journal[pid], classifier, scoring, today))

    return studies, stats, fetch_errors


def _print_harvest_summary(studies: list[dict], stats: list[JournalStat], fetch_errors: list[str]) -> None:
    print(f"NEW STUDIES: {len(studies)}")

    specialty_counts: dict[str, int] = {}
    for s in studies:
        for sp in s["specialties"]:
            specialty_counts[sp] = specialty_counts.get(sp, 0) + 1
    if specialty_counts:
        print("By specialty:")
        for sp, n in sorted(specialty_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {sp:28s} {n}")

    if stats:
        print("By journal:")
        for stat in sorted(stats, key=lambda s: s.name):
            if stat.error:
                print(f"  ! {stat.name} [{stat.ta}]: ERROR - {stat.error}")
                continue
            line = f"  {stat.name:45s} found={stat.found:4d} new={stat.new:4d}"
            if stat.truncated:
                line += f"  ** TRUNCATED: true count={stat.total_count} > retmax **"
            print(line)

    truncated = [s for s in stats if s.truncated]
    errored = [s for s in stats if s.error]
    if truncated:
        print(f"\nWARNING: {len(truncated)} journal(s) exceeded --max-per-journal - "
              f"coverage was silently cut off; raise --max-per-journal or narrow --days:")
        for s in truncated:
            print(f"  - {s.name} [{s.ta}]: true count {s.total_count} > retmax")
    if errored:
        print(f"\nWARNING: {len(errored)} journal(s) errored and were skipped this run:")
        for s in errored:
            print(f"  - {s.name} [{s.ta}]: {s.error}")
    if fetch_errors:
        print(f"\nWARNING: {len(fetch_errors)} efetch batch error(s) (affected PMIDs stay unseen "
              f"and will be retried next run):")
        for e in fetch_errors:
            print(f"  - {e}")


def _write_run_outputs(
    command: str, studies: list[dict], stats: list[JournalStat], fetch_errors: list[str],
    seen_date: str, window_days: int, target_date: str | None = None,
) -> None:
    by_date: dict[str, list[dict]] = {}
    for s in studies:
        by_date.setdefault(s["entryDate"], []).append(s)
    for date_str, day_studies in by_date.items():
        store.append_archive(date_str, day_studies)

    store.write_seen(seen_date, [s["pmid"] for s in studies])

    entry: dict = {
        "timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": command,
        "windowDays": window_days,
        "newCount": len(studies),
        "perJournal": {s.ta: {"found": s.found, "new": s.new, "error": s.error} for s in stats},
        "errors": fetch_errors,
    }
    if target_date is not None:
        entry["targetDate"] = target_date
    store.record_run(entry)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_harvest(args: argparse.Namespace) -> int:
    journals_cfg, taxonomy, scoring = config.load_all()
    classifier = classify_mod.build_classifier(taxonomy, scoring)
    today = dt.date.today()

    # A generous buffer beyond the query window itself, so a PMID that
    # appeared once (even if indexing lag briefly made a later run's reldate
    # window miss it) is never re-emitted as "new".
    seen_window = max(args.days + 30, 30)
    seen = store.load_seen(seen_window, today=today)

    def esearch_fn(term: str, retmax: int):
        return pubmed.esearch(term, reldate=args.days, retmax=retmax)

    studies, stats, fetch_errors = _run_harvest(
        journals_cfg, classifier, scoring,
        seen=seen, today=today, max_per_journal=args.max_per_journal,
        only_journal=args.only_journal, esearch_fn=esearch_fn,
    )

    _print_harvest_summary(studies, stats, fetch_errors)

    if args.dry_run:
        print("\n(dry run - archive, seen store, and run log were NOT written)")
        return 0

    _write_run_outputs("harvest", studies, stats, fetch_errors, today.isoformat(), args.days)

    if stats and all(s.error for s in stats):
        print("\nFAILED: every journal errored this run.", file=sys.stderr)
        return 1
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    try:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
    except ValueError as exc:
        print(f"ERROR: --start/--end must be YYYY-MM-DD ({exc})", file=sys.stderr)
        return 1
    if end < start:
        print("ERROR: --end must not be before --start", file=sys.stderr)
        return 1

    journals_cfg, taxonomy, scoring = config.load_all()
    classifier = classify_mod.build_classifier(taxonomy, scoring)

    total_new = 0
    any_hard_failure = False
    day = start
    while day <= end:
        print(f"\n--- backfilling {day.isoformat()} ---")
        seen = store.load_seen(BACKFILL_SEEN_LOOKBACK_DAYS, today=day)

        def esearch_fn(term: str, retmax: int, _day=day):
            iso = _day.isoformat()
            return pubmed.esearch(term, mindate=iso, maxdate=iso, retmax=retmax)

        studies, stats, fetch_errors = _run_harvest(
            journals_cfg, classifier, scoring,
            seen=seen, today=day, max_per_journal=args.max_per_journal,
            only_journal=None, esearch_fn=esearch_fn,
        )
        _print_harvest_summary(studies, stats, fetch_errors)
        total_new += len(studies)

        if not args.dry_run:
            _write_run_outputs(
                "backfill", studies, stats, fetch_errors, day.isoformat(), 1, target_date=day.isoformat()
            )

        if stats and all(s.error for s in stats):
            any_hard_failure = True

        day += dt.timedelta(days=1)

    n_days = (end - start).days + 1
    print(f"\nBACKFILL COMPLETE: {total_new} new studies across {n_days} day(s)")
    if args.dry_run:
        print("(dry run - nothing was written)")
    if any_hard_failure:
        print("FAILED: at least one day had every journal error.", file=sys.stderr)
        return 1
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    result = build_mod.build(window_days=args.window_days, site_url=args.site_url)
    print(
        f"BUILD OK: {result['totalStudies']} studies across {result['days']} day(s), "
        f"{result['topics']} topic files, {result['journals']} journals, "
        f"{result['highlights']} highlights, {result['searchIndexEntries']} search-index entries, "
        f"{result['feeds']} feed files (windowDays={result['windowDays']})"
    )
    return 0


def cmd_enrich_zh(args: argparse.Namespace) -> int:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.environ.get("CLOUDFLARE_AI_TOKEN")
    if not account_id or not api_token:
        print(
            "WARNING: Chinese enrichment skipped; "
            "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AI_TOKEN are required."
        )
        return 0

    client = CloudflareAIClient(account_id, api_token)
    result = enrich_mod.run(
        window_days=args.window_days, limit=args.limit, client=client
    )
    print(
        f"ZH ENRICHMENT: eligible={result.eligible} cached={result.cached} "
        f"generated={result.generated} failed={result.failed} excluded={result.excluded}"
    )
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    """Re-run classification, evidence detection, scoring and takeaway extraction
    over every archived record, in place, using the CURRENT config.

    This exists because topic assignments are computed at harvest time and stored
    in the archive, so editing taxonomy/*.json or scoring.json does NOT change
    what readers already see - a fixed rule would only ever apply to tomorrow's
    papers. That makes every misclassification permanent, which is untenable for a
    product whose whole improvement loop is readers reporting bad assignments.

    Run this after ANY edit to pipeline/config/, then run `build`. No network
    access: the archive already holds every parsed PubMed field, so this is a pure
    recomputation. Recency is recalculated against today, which also corrects the
    stale recency component baked into older records' scores at harvest time.
    """
    journals_cfg, taxonomy, scoring = config.load_all()
    by_ta = {j.ta: j for j in journals_cfg.journals}
    classifier = classify_mod.build_classifier(taxonomy, scoring)
    today = dt.date.today()

    dates = store._archive_dates()
    if not dates:
        print("Nothing to reclassify: data/archive is empty. Run `harvest` first.")
        return 1

    total = changed_topics = changed_evidence = changed_score = orphaned = 0
    for date_str in dates:
        records = store.read_archive_day(date_str)
        rebuilt: list[dict] = []
        for record in records:
            total += 1
            journal = by_ta.get((record.get("journal") or {}).get("ta", ""))
            if journal is None:
                # The journal was removed from the registry after this record was
                # archived. Keep the record exactly as-is rather than dropping a
                # study a reader may have bookmarked, and report the count.
                orphaned += 1
                rebuilt.append(record)
                continue
            fresh = _assemble_study(record, journal, classifier, scoring, today)
            if fresh["topics"] != record.get("topics"):
                changed_topics += 1
            if fresh["evidence"]["level"] != (record.get("evidence") or {}).get("level"):
                changed_evidence += 1
            if fresh["score"] != record.get("score"):
                changed_score += 1
            rebuilt.append(fresh)

        if args.dry_run:
            continue
        store._write_jsonl_gz(store.archive_path(date_str), rebuilt)

    verb = "would change" if args.dry_run else "changed"
    print(f"RECLASSIFY {'(dry run) ' if args.dry_run else ''}over {len(dates)} archive day(s), {total} records:")
    print(f"  topics {verb}     : {changed_topics}")
    print(f"  evidence {verb}   : {changed_evidence}")
    print(f"  score {verb}      : {changed_score}")
    if orphaned:
        print(f"  left untouched   : {orphaned} (journal no longer in journals.json)")
    if not args.dry_run:
        print("Now run `build` to regenerate data/api and data/feeds.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    journals_cfg, _taxonomy, _scoring = config.load_all()
    print(f"Journal resolution check over {args.days}d window (datetype=edat):\n")
    problems: list[str] = []
    for journal in journals_cfg.journals:
        term = _build_term(journal, journals_cfg.topic_filter)
        try:
            _ids, count, translation = pubmed.esearch(term, reldate=args.days, retmax=1)
        except pubmed.PubMedError as exc:
            print(f"  ERROR  {journal.name} [{journal.ta}]: {exc}")
            problems.append(journal.name)
            continue

        flags = []
        if count == 0:
            flags.append("ZERO HITS")
        if "[Journal]" not in translation:
            flags.append("QUERY DID NOT RESOLVE TO A [Journal] TAG")
        flag_str = f"   <-- {', '.join(flags)}" if flags else ""
        print(f"  {count:>6}  {journal.name}  [{journal.ta}]{flag_str}")
        if flags:
            problems.append(journal.name)

    print()
    if problems:
        print(f"{len(problems)} journal(s) need review:")
        for name in problems:
            print(f"  - {name}")
        print(
            "\nVerify the abbreviation at "
            "https://www.ncbi.nlm.nih.gov/nlmcatalog/journals and update pipeline/config/journals.json."
        )
        return 1
    print("All journals resolved cleanly.")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        journals_cfg, taxonomy, scoring = config.load_all()
    except ConfigError as exc:
        print(f"INVALID CONFIG: {exc}", file=sys.stderr)
        return 1
    n_topics = len(taxonomy.all_topics)
    print(
        f"OK: {len(journals_cfg.journals)} journals, {len(taxonomy.specialties)} specialties, "
        f"{n_topics} topics, scoring config v{scoring.version}"
    )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    days = store.archive_day_coverage()
    total_bytes = store.archive_total_bytes()
    seen_n = store.seen_count()
    run = store.last_run()

    print(f"archive: {len(days)} day file(s), {total_bytes / 1024:.1f} KiB")
    if days:
        print(f"  coverage: {days[0]} .. {days[-1]}")
    print(f"seen PMIDs (union of all seen/*.txt files): {seen_n}")
    if run:
        print(
            f"last run: {run.get('timestamp')} ({run.get('command')}) "
            f"new={run.get('newCount')} windowDays={run.get('windowDays')}"
        )
    else:
        print("last run: none")
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evidence_digest.cli",
        description="Evidence Digest harvest/build/validate pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("harvest", help="query every journal, fetch+classify+score new PMIDs, write archive")
    p.add_argument("--days", type=int, default=3, help="reldate window in days (default 3)")
    p.add_argument("--max-per-journal", type=int, default=DEFAULT_MAX_PER_JOURNAL, help="esearch retmax per journal")
    p.add_argument("--dry-run", action="store_true", help="print the summary but write nothing")
    p.add_argument("--only-journal", default=None, metavar="TA", help="restrict to one journal's MEDLINE ta")
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser("build", help="rebuild data/api and data/feeds from the committed archive")
    p.add_argument("--window-days", type=int, default=None, help="default: scoring.json limits.servedWindowDays")
    p.add_argument("--site-url", default="", help="base URL used for feed self/alternate links")
    p.set_defaults(func=cmd_build)

    enrich_zh = sub.add_parser(
        "enrich-zh", help="Generate cached Chinese summaries"
    )
    enrich_zh.add_argument("--window-days", type=int, default=120)
    enrich_zh.add_argument("--limit", type=int, default=200)
    enrich_zh.set_defaults(func=cmd_enrich_zh)

    p = sub.add_parser(
        "reclassify",
        help="re-apply current config to the existing archive, in place (no network) — run after editing taxonomy or scoring, then `build`",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many records would change without rewriting anything",
    )
    p.set_defaults(func=cmd_reclassify)

    p = sub.add_parser("check", help="validate every journal ta against live PubMed (network)")
    p.add_argument("--days", type=int, default=120)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("validate", help="validate the three config files, no network")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("stats", help="show archive size, seen count, day coverage, last run")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("backfill", help="harvest a historical window in day-sized chunks")
    p.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive")
    p.add_argument("--end", required=True, help="YYYY-MM-DD, inclusive")
    p.add_argument("--max-per-journal", type=int, default=DEFAULT_MAX_PER_JOURNAL)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_backfill)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 1
    except pubmed.PubMedError as exc:
        print(f"PUBMED ERROR: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        # argparse raises this for --help etc; our own raises above carry a message.
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        return exc.code or 0


if __name__ == "__main__":
    sys.exit(main())
