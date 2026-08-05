"""Tests for the dated, deterministic Chinese-summary enrichment cache."""

from __future__ import annotations

import datetime as dt
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from evidence_digest import enrichment_store
from evidence_digest.config import Paths
from evidence_digest.zh_summary import MODEL_ID, PROMPT_VERSION


def _scratch_paths(root: Path) -> Paths:
    data_dir = root / "data"
    state_dir = data_dir / "state"
    return Paths(
        repo_root=root, pipeline_dir=root, config_dir=root / "config",
        taxonomy_dir=root / "config" / "taxonomy",
        journals_path=root / "config" / "journals.json",
        scoring_path=root / "config" / "scoring.json",
        contract_dir=root / "contract",
        data_dir=data_dir, archive_dir=data_dir / "archive",
        state_dir=state_dir, seen_dir=state_dir / "seen",
        runs_path=state_dir / "runs.json",
        api_dir=data_dir / "api", feeds_dir=data_dir / "feeds",
    )


def _record(pmid: str, source_hash: str = "source-a") -> dict:
    return {
        "pmid": pmid,
        "sourceHash": source_hash,
        "model": MODEL_ID,
        "promptVersion": PROMPT_VERSION,
        "generatedAt": "2026-08-05T12:34:56Z",
        "objective": "评估治疗效果",
        "methods": "进行随机对照试验",
        "results": "治疗组结局改善",
        "conclusion": "治疗可能有效",
    }


class EnrichmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = _scratch_paths(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_uses_dated_path_and_is_valid_deterministic_gzip_jsonl(self) -> None:
        day = dt.date(2026, 8, 5)
        records = [_record("20", "hash-b"), _record("3", "hash-c"), _record("100", "hash-a")]

        self.assertEqual(enrichment_store.write_day(day, records, self.paths), 3)
        path = self.paths.data_dir / "enrichment" / "zh" / "2026-08-05.jsonl.gz"
        self.assertEqual(enrichment_store.cache_dir(self.paths), path.parent)
        self.assertEqual(enrichment_store.cache_path(day, self.paths), path)
        raw_first = path.read_bytes()
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            written = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual([record["pmid"] for record in written], ["3", "20", "100"])

        enrichment_store.write_day(day, list(reversed(records)), self.paths)
        self.assertEqual(path.read_bytes(), raw_first)

    def test_read_latest_prefers_the_later_dated_valid_record(self) -> None:
        enrichment_store.write_day(dt.date(2026, 8, 4), [_record("1", "old")], self.paths)
        enrichment_store.write_day(dt.date(2026, 8, 5), [_record("1", "new"), _record("2")], self.paths)

        latest = enrichment_store.read_latest(self.paths)

        self.assertEqual(set(latest), {"1", "2"})
        self.assertEqual(latest["1"]["sourceHash"], "new")

    def test_write_day_replaces_a_same_day_duplicate_instead_of_adding_a_line(self) -> None:
        day = dt.date(2026, 8, 5)
        enrichment_store.write_day(day, [_record("1", "older")], self.paths)

        self.assertEqual(enrichment_store.write_day(day, [_record("1", "newer")], self.paths), 1)
        with gzip.open(enrichment_store.cache_path(day, self.paths), "rt", encoding="utf-8") as fh:
            written = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(written, [_record("1", "newer")])

    def test_read_latest_skips_bad_lines_and_invalid_records_without_losing_valid_neighbors(self) -> None:
        day = dt.date(2026, 8, 5)
        path = enrichment_store.cache_path(day, self.paths)
        path.parent.mkdir(parents=True)
        invalid = _record("2")
        invalid["results"] = ""
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(_record("1"), ensure_ascii=False) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps(invalid, ensure_ascii=False) + "\n")
            fh.write(json.dumps(_record("3"), ensure_ascii=False) + "\n")

        self.assertEqual(enrichment_store.read_latest(self.paths), {"1": _record("1"), "3": _record("3")})

    def test_write_day_ignores_invalid_incoming_summary_records(self) -> None:
        invalid = _record("2")
        invalid["objective"] = ""

        self.assertEqual(enrichment_store.write_day(dt.date(2026, 8, 5), [_record("1"), invalid], self.paths), 1)
        self.assertEqual(enrichment_store.read_latest(self.paths), {"1": _record("1")})

    def test_write_day_skips_a_nonnumeric_pmid_without_blocking_valid_records(self) -> None:
        self.assertEqual(
            enrichment_store.write_day(
                dt.date(2026, 8, 5), [_record("1"), _record("not-a-pmid")], self.paths
            ),
            1,
        )
        self.assertEqual(enrichment_store.read_latest(self.paths), {"1": _record("1")})


if __name__ == "__main__":
    unittest.main()
