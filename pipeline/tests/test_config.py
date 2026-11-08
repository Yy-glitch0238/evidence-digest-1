"""config.py tests. Two flavours:

* Against the REAL pipeline/config/*.json - other agents are actively adding
  taxonomy files, so this doubles as a live check that whatever is currently
  committed loads and cross-validates cleanly.
* Against small synthetic config trees built in a temp directory, to exercise
  every failure path (duplicate topic slug, missing catchAll, unresolved
  journal specialty, slug/filename mismatch) without depending on anyone
  breaking the real config on purpose.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_PIPELINE_DIR = str(_Path(__file__).resolve().parent.parent)
if _PIPELINE_DIR not in _sys.path:
    _sys.path.insert(0, _PIPELINE_DIR)

import json
import tempfile
import unittest
from pathlib import Path

from evidence_digest.config import ConfigError, Paths, load_all, load_journals, load_scoring, load_taxonomy
from evidence_digest.cli import _build_term


class RealConfigTests(unittest.TestCase):
    """No network: these read local JSON files already committed to the repo."""

    def test_real_config_loads_and_cross_validates(self) -> None:
        journals_cfg, taxonomy, scoring = load_all()
        self.assertGreater(len(journals_cfg.journals), 0)
        self.assertGreater(len(taxonomy.specialties), 0)
        self.assertGreater(len(taxonomy.all_topics), 0)
        self.assertEqual(scoring.assign_threshold, scoring.assign_threshold)  # loads without raising

    def test_specialties_sorted_by_order(self) -> None:
        _journals, taxonomy, _scoring = load_all()
        orders = [s.order for s in taxonomy.specialties]
        self.assertEqual(orders, sorted(orders))

    def test_exactly_one_catch_all_per_specialty(self) -> None:
        _journals, taxonomy, _scoring = load_all()
        for specialty in taxonomy.specialties:
            catch_alls = [t for t in specialty.topics if t.catch_all]
            self.assertEqual(len(catch_alls), 1, f"specialty {specialty.slug} has {len(catch_alls)} catchAll topics")

    def test_topic_slugs_globally_unique(self) -> None:
        _journals, taxonomy, _scoring = load_all()
        slugs = [t.slug for t in taxonomy.all_topics]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_every_journal_specialty_resolves(self) -> None:
        journals_cfg, taxonomy, _scoring = load_all()
        known = set(taxonomy.specialty_by_slug)
        for journal in journals_cfg.journals:
            self.assertIn(journal.specialty, known)


# --------------------------------------------------------------------------- #
# synthetic config trees for failure-path coverage
# --------------------------------------------------------------------------- #


VALID_SCORING = {
    "version": 1,
    "classifier": {"assignThreshold": 3, "weights": {"mesh": 3, "title": 3, "keywords": 2, "abstract": 1}},
    "journalTier": {"1": 26, "2": 16, "3": 9},
    "evidenceLevels": {
        "guideline": {"rank": 1, "label": "Guideline", "points": 34},
        "meta-analysis": {"rank": 2, "label": "Meta-analysis", "points": 30},
        "rct": {"rank": 3, "label": "Randomized trial", "points": 32},
        "trial": {"rank": 4, "label": "Clinical trial", "points": 24},
        "observational": {"rank": 5, "label": "Observational", "points": 16},
        "review": {"rank": 6, "label": "Review", "points": 10},
        "basic": {"rank": 7, "label": "Preclinical", "points": 6},
        "case-report": {"rank": 8, "label": "Case report", "points": 3},
        "other": {"rank": 9, "label": "Other", "points": 0},
    },
    "bonuses": {"phase3": 10},
    "penalties": {"noAbstract": 8},
    "recency": {"maxPoints": 18, "halfLifeDays": 10},
    "limits": {
        "servedWindowDays": 120, "perTopicFile": 400, "highlights": 40, "highlightsPerSpecialty": 4,
        "searchIndexMinScore": 55, "searchIndexWindowDays": 90, "feedItems": 50,
        "emailStudiesPerTopic": 6, "emailMaxStudies": 30,
    },
}


def _minimal_topic(slug: str, catch_all: bool = False) -> dict:
    return {"slug": slug, "name": slug, "blurb": "", "catchAll": catch_all,
            "rules": {"mesh": [], "phrases": [], "acronyms": [], "not": []}}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class SyntheticConfigTree:
    """Builds a scratch pipeline/config-shaped tree and returns a Paths
    pointing at it, so each failure-path test starts from a known-good base
    and mutates exactly one thing."""

    def __init__(self, tmp_dir: Path) -> None:
        self.root = tmp_dir
        self.config_dir = tmp_dir / "config"
        self.taxonomy_dir = self.config_dir / "taxonomy"
        self.paths = Paths(
            repo_root=tmp_dir,
            pipeline_dir=tmp_dir,
            config_dir=self.config_dir,
            taxonomy_dir=self.taxonomy_dir,
            journals_path=self.config_dir / "journals.json",
            scoring_path=self.config_dir / "scoring.json",
            contract_dir=tmp_dir / "contract",
            data_dir=tmp_dir / "data",
            archive_dir=tmp_dir / "data" / "archive",
            state_dir=tmp_dir / "data" / "state",
            seen_dir=tmp_dir / "data" / "state" / "seen",
            runs_path=tmp_dir / "data" / "state" / "runs.json",
            api_dir=tmp_dir / "data" / "api",
            feeds_dir=tmp_dir / "data" / "feeds",
        )

    def write_scoring(self, data: dict = VALID_SCORING) -> None:
        _write(self.paths.scoring_path, data)

    def write_journals(self, journals: list[dict], topic_filter: str = "clinical[tiab]") -> None:
        _write(self.paths.journals_path, {"version": 1, "topicFilter": topic_filter, "journals": journals})

    def write_specialty(self, filename: str, data: dict) -> None:
        _write(self.taxonomy_dir / filename, data)


class TaxonomyFailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = SyntheticConfigTree(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_duplicate_topic_slug_across_files_names_both_files(self) -> None:
        self.tree.write_specialty("heme.json", {
            "slug": "heme", "order": 10, "name": "Heme", "icon": "x", "blurb": "",
            "topics": [_minimal_topic("shared-slug", catch_all=True)],
        })
        self.tree.write_specialty("onc.json", {
            "slug": "onc", "order": 20, "name": "Onc", "icon": "x", "blurb": "",
            "topics": [_minimal_topic("shared-slug"), _minimal_topic("onc-other", catch_all=True)],
        })
        with self.assertRaises(ConfigError) as ctx:
            load_taxonomy(self.tree.paths)
        message = str(ctx.exception)
        self.assertIn("heme.json", message)
        self.assertIn("onc.json", message)
        self.assertIn("shared-slug", message)

    def test_missing_catch_all_raises(self) -> None:
        self.tree.write_specialty("heme.json", {
            "slug": "heme", "order": 10, "name": "Heme", "icon": "x", "blurb": "",
            "topics": [_minimal_topic("heme-a")],
        })
        with self.assertRaises(ConfigError) as ctx:
            load_taxonomy(self.tree.paths)
        self.assertIn("catchAll", str(ctx.exception))

    def test_two_catch_alls_raises(self) -> None:
        self.tree.write_specialty("heme.json", {
            "slug": "heme", "order": 10, "name": "Heme", "icon": "x", "blurb": "",
            "topics": [_minimal_topic("heme-a", catch_all=True), _minimal_topic("heme-b", catch_all=True)],
        })
        with self.assertRaises(ConfigError):
            load_taxonomy(self.tree.paths)

    def test_slug_filename_mismatch_raises(self) -> None:
        self.tree.write_specialty("heme.json", {
            "slug": "wrong-slug", "order": 10, "name": "Heme", "icon": "x", "blurb": "",
            "topics": [_minimal_topic("heme-a", catch_all=True)],
        })
        with self.assertRaises(ConfigError) as ctx:
            load_taxonomy(self.tree.paths)
        self.assertIn("filename stem", str(ctx.exception))

    def test_bad_slug_pattern_raises(self) -> None:
        self.tree.write_specialty("heme.json", {
            "slug": "heme", "order": 10, "name": "Heme", "icon": "x", "blurb": "",
            "topics": [_minimal_topic("Heme_AML", catch_all=True)],
        })
        with self.assertRaises(ConfigError):
            load_taxonomy(self.tree.paths)


class JournalsFailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = SyntheticConfigTree(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unresolved_specialty_raises_via_load_all(self) -> None:
        self.tree.write_specialty("heme.json", {
            "slug": "heme", "order": 10, "name": "Heme", "icon": "x", "blurb": "",
            "topics": [_minimal_topic("heme-a", catch_all=True)],
        })
        self.tree.write_journals([
            {"name": "X", "ta": "X", "specialty": "does-not-exist", "tier": 1, "scope": "all"}
        ])
        self.tree.write_scoring()
        with self.assertRaises(ConfigError) as ctx:
            load_all(self.tree.paths)
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_bad_tier_raises(self) -> None:
        self.tree.write_journals([
            {"name": "X", "ta": "X", "specialty": "heme", "tier": 7, "scope": "all"}
        ])
        with self.assertRaises(ConfigError):
            load_journals(self.tree.paths)

    def test_bad_scope_raises(self) -> None:
        self.tree.write_journals([
            {"name": "X", "ta": "X", "specialty": "heme", "tier": 1, "scope": "everything"}
        ])
        with self.assertRaises(ConfigError):
            load_journals(self.tree.paths)

    def test_virus_scope_requires_filter(self) -> None:
        self.tree.write_journals([
            {"name": "X", "ta": "X", "specialty": "heme", "tier": 1, "scope": "virus"}
        ])
        with self.assertRaisesRegex(ConfigError, "virusFilter"):
            load_journals(self.tree.paths)

    def test_virus_scope_uses_distinct_query(self) -> None:
        journals = [
            {"name": "X", "ta": "X", "specialty": "heme", "tier": 1, "scope": "virus"},
            {"name": "Y", "ta": "Y", "specialty": "heme", "tier": 1, "scope": "topic"},
            {"name": "Z", "ta": "Z", "specialty": "heme", "tier": 1, "scope": "all"},
        ]
        _write(self.tree.paths.journals_path, {
            "version": 1,
            "topicFilter": "immun*[tiab]",
            "virusFilter": "(virus*[tiab] OR phage*[tiab])",
            "journals": journals,
        })
        cfg = load_journals(self.tree.paths)
        terms = [_build_term(j, cfg.topic_filter, cfg.virus_filter) for j in cfg.journals]
        self.assertEqual(terms, [
            '("X"[ta]) AND (virus*[tiab] OR phage*[tiab])',
            '("Y"[ta]) AND immun*[tiab]',
            '"Z"[ta]',
        ])

    def test_virus_scope_rejects_empty_filter(self) -> None:
        _write(self.tree.paths.journals_path, {
            "version": 1,
            "topicFilter": "immun*[tiab]",
            "virusFilter": " ",
            "journals": [{"name": "X", "ta": "X", "specialty": "heme", "tier": 1, "scope": "virus"}],
        })
        with self.assertRaisesRegex(ConfigError, "virusFilter"):
            load_journals(self.tree.paths)


class ScoringFailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = SyntheticConfigTree(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ignores_underscore_comment_keys_in_nested_objects(self) -> None:
        data = json.loads(json.dumps(VALID_SCORING))
        data["journalTier"]["_comment"] = "some doc string"
        data["bonuses"]["_comment"] = "some doc string"
        data["limits"]["_comment"] = "some doc string"
        self.tree.write_scoring(data)
        scoring = load_scoring(self.tree.paths)
        self.assertEqual(scoring.journal_tier, {1: 26, 2: 16, 3: 9})

    def test_duplicate_evidence_rank_raises(self) -> None:
        data = json.loads(json.dumps(VALID_SCORING))
        data["evidenceLevels"]["review"]["rank"] = 1  # collides with "guideline"
        self.tree.write_scoring(data)
        with self.assertRaises(ConfigError):
            load_scoring(self.tree.paths)

    def test_missing_evidence_level_raises(self) -> None:
        data = json.loads(json.dumps(VALID_SCORING))
        del data["evidenceLevels"]["case-report"]
        self.tree.write_scoring(data)
        with self.assertRaises(ConfigError):
            load_scoring(self.tree.paths)

    def test_missing_limits_key_raises(self) -> None:
        data = json.loads(json.dumps(VALID_SCORING))
        del data["limits"]["feedItems"]
        self.tree.write_scoring(data)
        with self.assertRaises(ConfigError):
            load_scoring(self.tree.paths)


if __name__ == "__main__":
    unittest.main()
