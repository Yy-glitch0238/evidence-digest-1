"""build.py tests against a synthetic archive (passed in directly, not read
from disk) and the REAL taxonomy/journals/scoring config, so output files
only need api/data/feeds paths to be scratch. This exercises the actual
production caps and topic/specialty structure while staying fully offline."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_PIPELINE_DIR = str(_Path(__file__).resolve().parent.parent)
if _PIPELINE_DIR not in _sys.path:
    _sys.path.insert(0, _PIPELINE_DIR)

import datetime as dt
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from evidence_digest import build, enrichment_store, zh_summary
from evidence_digest.config import PATHS, Paths, load_journals, load_taxonomy


def _scratch_data_paths(root: Path) -> Paths:
    """Reuse the REAL config/ tree but redirect data/api/feeds to scratch."""
    data_dir = root / "data"
    return Paths(
        repo_root=PATHS.repo_root, pipeline_dir=PATHS.pipeline_dir, config_dir=PATHS.config_dir,
        taxonomy_dir=PATHS.taxonomy_dir, journals_path=PATHS.journals_path, scoring_path=PATHS.scoring_path,
        contract_dir=PATHS.contract_dir,
        data_dir=data_dir, archive_dir=data_dir / "archive",
        state_dir=data_dir / "state", seen_dir=data_dir / "state" / "seen",
        runs_path=data_dir / "state" / "runs.json",
        api_dir=data_dir / "api", feeds_dir=data_dir / "feeds",
    )


def _pick_topic_and_journal():
    taxonomy = load_taxonomy()
    journals_cfg = load_journals()
    specialty = next(
        s for s in taxonomy.specialties
        if any(not topic.catch_all for topic in s.topics)
    )
    topic = next(topic for topic in specialty.topics if not topic.catch_all)
    catch_all = specialty.catch_all_topic
    journal = journals_cfg.journals[0]
    return taxonomy, specialty, topic, catch_all, journal


def _full_study(pmid: str, entry_date: str, score: int, topic_slug: str, specialty_slug: str, journal) -> dict:
    return {
        "pmid": pmid, "doi": None, "title": f"Study about topic {topic_slug} #{pmid}",
        "abstract": "BACKGROUND: x. CONCLUSIONS: This changes practice.",
        "sections": {"BACKGROUND": "x", "CONCLUSIONS": "This changes practice."},
        "takeaway": "This changes practice.",
        "authors": ["Smith J"], "authorLine": "Smith J",
        "journal": {"name": journal.name, "ta": journal.ta, "tier": journal.tier},
        "specialties": [specialty_slug], "topics": [topic_slug],
        "pubTypes": ["Randomized Controlled Trial"],
        "evidence": {"level": "rct", "label": "Randomized trial", "rank": 3},
        "score": score, "pubdate": entry_date, "entryDate": entry_date,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "doiUrl": None,
        "pmcid": None, "openAccess": False,
        "mesh": ["Some Descriptor"], "keywords": ["kw"], "trialIds": [], "hasAbstract": True,
    }


def _valid_zh_summary(study: dict) -> dict:
    return {
        "pmid": study["pmid"],
        "sourceHash": zh_summary.source_hash(study),
        "model": zh_summary.MODEL_ID,
        "promptVersion": zh_summary.PROMPT_VERSION,
        "generatedAt": "2026-07-24T12:34:56Z",
        "objective": "评估研究目的。",
        "methods": "分析研究方法。",
        "results": "报告主要结果。",
        "conclusion": "总结研究结论。",
    }


class BuildOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.taxonomy, cls.specialty, cls.topic, cls.catch_all, cls.journal = _pick_topic_and_journal()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = _scratch_data_paths(Path(self._tmp.name))
        self.today = dt.date(2026, 7, 24)

        self.records = [
            _full_study("101", "2026-07-24", 90, self.topic.slug, self.specialty.slug, self.journal),
            _full_study("102", "2026-07-23", 70, self.topic.slug, self.specialty.slug, self.journal),
            _full_study("103", "2026-07-20", 95, self.catch_all.slug, self.specialty.slug, self.journal),
        ]
        self.result = build.build(
            window_days=30, site_url="https://example.org", paths=self.paths, today=self.today,
            records=self.records,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _read_json(self, relpath: str):
        return json.loads((self.paths.api_dir / relpath).read_text(encoding="utf-8"))

    def test_manifest_shape_and_counts(self) -> None:
        manifest = self._read_json("manifest.json")
        for key in ("dataVersion", "generatedAt", "windowDays", "days", "totalStudies",
                     "journalCount", "topicCounts", "specialtyCounts", "latestDay"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["totalStudies"], 3)
        self.assertEqual(manifest["latestDay"], "2026-07-24")
        self.assertEqual(manifest["days"], ["2026-07-24", "2026-07-23", "2026-07-20"])
        self.assertEqual(manifest["topicCounts"][self.topic.slug], 2)  # 101, 102

    def test_topic_file_written_for_every_topic_even_empty(self) -> None:
        # A topic nobody's synthetic study touched must still exist so the
        # web app never 404s fetching a reader-selected topic.
        untouched_specialty = self.taxonomy.specialties[-1]
        untouched_topic = untouched_specialty.topics[0]
        path = self.paths.api_dir / "topics" / f"{untouched_topic.slug}.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(data["returned"], 0)
        self.assertEqual(data["studies"], [])

    def test_topic_file_cards_are_lean_and_sorted(self) -> None:
        data = self._read_json(f"topics/{self.topic.slug}.json")
        self.assertEqual(data["total"], 2)
        pmids = [s["pmid"] for s in data["studies"]]
        self.assertEqual(pmids, ["101", "102"])  # newest entryDate first
        for study in data["studies"]:
            for stripped_key in ("abstract", "sections", "mesh", "keywords"):
                self.assertNotIn(stripped_key, study)
            self.assertIn("takeaway", study)
            self.assertIn("score", study)

    def test_day_files_contain_full_studies(self) -> None:
        data = self._read_json("days/2026-07-24.json")
        self.assertEqual(data["total"], 1)
        study = data["studies"][0]
        self.assertIn("abstract", study)
        self.assertIn("mesh", study)

    def test_highlights_within_7_days_only(self) -> None:
        data = self._read_json("highlights.json")
        pmids = {s["pmid"] for s in data["studies"]}
        # pmid 103 is entryDate 2026-07-20, 4 days before "today" 2026-07-24: within 7.
        self.assertIn("101", pmids)
        self.assertIn("103", pmids)

    def test_search_index_only_above_min_score(self) -> None:
        data = self._read_json("search-index.json")
        min_score = data["minScore"]
        for entry in data["entries"]:
            self.assertGreaterEqual(entry["s"], min_score)
            self.assertEqual(set(entry), {"p", "t", "j", "d", "s", "tp"})

    def test_public_taxonomy_strips_rules(self) -> None:
        data = self._read_json("taxonomy.json")
        self.assertIn("specialties", data)
        for specialty in data["specialties"]:
            self.assertEqual(set(specialty), {"slug", "name", "icon", "blurb", "topics"})
            for topic in specialty["topics"]:
                self.assertEqual(set(topic), {"slug", "name", "blurb"})

    def test_public_journals_file(self) -> None:
        data = self._read_json("journals.json")
        self.assertEqual(data["count"], len(data["journals"]))
        for j in data["journals"]:
            self.assertTrue(j["pubmedUrl"].startswith("https://pubmed.ncbi.nlm.nih.gov/"))

    def test_nojekyll_written(self) -> None:
        self.assertTrue((self.paths.api_dir / ".nojekyll").exists())

    def test_feeds_written_and_parseable(self) -> None:
        all_feed = self.paths.feeds_dir / "all.xml"
        self.assertTrue(all_feed.exists())
        tree = ET.parse(all_feed)  # must not raise
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = tree.getroot().findall("a:entry", ns)
        self.assertEqual(len(entries), 3)

    def test_rebuild_is_byte_identical(self) -> None:
        first = (self.paths.api_dir / f"topics/{self.topic.slug}.json").read_bytes()
        build.build(window_days=30, site_url="https://example.org", paths=self.paths, today=self.today,
                    records=self.records)
        second = (self.paths.api_dir / f"topics/{self.topic.slug}.json").read_bytes()
        self.assertEqual(first, second)

    def test_manifest_is_pretty_printed(self) -> None:
        raw = (self.paths.api_dir / "manifest.json").read_text(encoding="utf-8")
        self.assertIn("\n", raw)

    def test_result_summary_counts(self) -> None:
        self.assertEqual(self.result["totalStudies"], 3)
        self.assertEqual(self.result["days"], 3)
        self.assertGreater(self.result["topics"], 0)


class EmptyArchiveBuildTests(unittest.TestCase):
    def test_build_with_no_records_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _scratch_data_paths(Path(tmp))
            result = build.build(window_days=30, site_url="", paths=paths, today=dt.date(2026, 7, 24), records=[])
            self.assertEqual(result["totalStudies"], 0)
            manifest = json.loads((paths.api_dir / "manifest.json").read_text())
            self.assertEqual(manifest["days"], [])
            self.assertEqual(manifest["latestDay"], "2026-07-24")


class ChineseFeedBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.taxonomy, cls.specialty, cls.topic, _, cls.journal = _pick_topic_and_journal()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = _scratch_data_paths(Path(self._tmp.name))
        self.today = dt.date(2026, 7, 24)

        eligible = [
            _full_study(
                str(20000000 + index),
                "2026-07-24" if index <= 100 else "2026-07-23",
                index,
                self.topic.slug,
                self.specialty.slug,
                self.journal,
            )
            for index in range(202)
        ]
        self.eligible = eligible

        excluded = _full_study(
            "90000001", "2026-07-24", 999, self.topic.slug, self.specialty.slug, self.journal
        )
        excluded["pubTypes"] = ["Editorial"]
        removed_journal = _full_study(
            "90000002", "2026-07-24", 999, self.topic.slug, self.specialty.slug, self.journal
        )
        removed_journal["journal"] = {
            "name": "Removed Journal", "ta": "Removed J", "tier": 1
        }
        no_abstract = _full_study(
            "90000003", "2026-07-24", 999, self.topic.slug, self.specialty.slug, self.journal
        )
        no_abstract["hasAbstract"] = False
        no_abstract["abstract"] = ""
        stale = _full_study(
            "90000004", "2026-07-24", 999, self.topic.slug, self.specialty.slug, self.journal
        )
        missing = _full_study(
            "90000005", "2026-07-24", 999, self.topic.slug, self.specialty.slug, self.journal
        )
        self.ineligible = [excluded, removed_journal, no_abstract, stale, missing]
        self.records = [*eligible, dict(eligible[0]), *self.ineligible]

        summaries = [_valid_zh_summary(study) for study in eligible]
        summaries.extend(_valid_zh_summary(study) for study in self.ineligible[:3])
        stale_summary = _valid_zh_summary(stale)
        stale_summary["sourceHash"] = "0" * 64
        summaries.append(stale_summary)
        enrichment_store.write_day(self.today, summaries, self.paths)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self) -> dict:
        with mock.patch("evidence_digest.build._now_iso", return_value="2026-07-24T13:00:00Z"):
            return build.build(
                window_days=30,
                site_url="https://example.org",
                paths=self.paths,
                today=self.today,
                records=self.records,
            )

    def _zh_entry_pmids(self) -> list[str]:
        path = self.paths.feeds_dir / "selected-journals-zh.xml"
        root = ET.parse(path).getroot()
        ns = {"a": "http://www.w3.org/2005/Atom"}
        return [
            entry.find("a:id", ns).text.removeprefix("urn:evidence-digest:zh:")
            for entry in root.findall("a:entry", ns)
        ]

    def test_build_writes_only_eligible_current_summaries(self) -> None:
        result = self._build()
        path = self.paths.feeds_dir / "selected-journals-zh.xml"
        self.assertTrue(path.exists())
        pmids = self._zh_entry_pmids()
        self.assertIn("20000000", pmids)
        for study in self.ineligible:
            self.assertNotIn(study["pmid"], pmids)
        self.assertEqual(result["feeds"], len(self.taxonomy.all_topics) + 2)

    def test_chinese_feed_uses_build_sort_deduplicates_and_caps_at_200(self) -> None:
        self._build()
        pmids = self._zh_entry_pmids()
        expected_indices = [*range(100, -1, -1), *range(201, 102, -1)]
        self.assertEqual(pmids, [str(20000000 + index) for index in expected_indices])
        self.assertEqual(len(pmids), 200)
        self.assertEqual(len(pmids), len(set(pmids)))

    def test_chinese_feed_rebuild_is_byte_identical(self) -> None:
        self._build()
        path = self.paths.feeds_dir / "selected-journals-zh.xml"
        first = path.read_bytes()
        self._build()
        self.assertEqual(path.read_bytes(), first)

    def test_enrichment_cache_does_not_change_existing_english_feed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _scratch_data_paths(Path(tmp))
            records = self.eligible[:3]
            with mock.patch("evidence_digest.build._now_iso", return_value="2026-07-24T13:00:00Z"):
                build.build(
                    window_days=30, site_url="https://example.org", paths=paths,
                    today=self.today, records=records,
                )
            before = {
                path.name: path.read_bytes()
                for path in paths.feeds_dir.glob("*.xml")
                if path.name != "selected-journals-zh.xml"
            }

            enrichment_store.write_day(
                self.today, [_valid_zh_summary(study) for study in records], paths
            )
            with mock.patch("evidence_digest.build._now_iso", return_value="2026-07-24T13:00:00Z"):
                build.build(
                    window_days=30, site_url="https://example.org", paths=paths,
                    today=self.today, records=records,
                )
            after = {
                path.name: path.read_bytes()
                for path in paths.feeds_dir.glob("*.xml")
                if path.name != "selected-journals-zh.xml"
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
