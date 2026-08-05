"""Tests for Chinese-summary orchestration and its CLI entry point."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evidence_digest import enrich, enrichment_store
from evidence_digest.cloudflare_ai import CloudflareAIError
from evidence_digest.config import Paths
from evidence_digest.zh_summary import MODEL_ID, PROMPT_VERSION, source_hash


TODAY = dt.date(2026, 8, 5)
SUMMARY = {
    "objective": "评估治疗效果",
    "methods": "进行随机对照试验",
    "results": "治疗组结局改善",
    "conclusion": "治疗可能有效",
}


def _scratch_paths(root: Path) -> Paths:
    data_dir = root / "data"
    state_dir = data_dir / "state"
    return Paths(
        repo_root=root,
        pipeline_dir=root,
        config_dir=root / "config",
        taxonomy_dir=root / "config" / "taxonomy",
        journals_path=root / "config" / "journals.json",
        scoring_path=root / "config" / "scoring.json",
        contract_dir=root / "contract",
        data_dir=data_dir,
        archive_dir=data_dir / "archive",
        state_dir=state_dir,
        seen_dir=state_dir / "seen",
        runs_path=state_dir / "runs.json",
        api_dir=data_dir / "api",
        feeds_dir=data_dir / "feeds",
    )


def _study(pmid: str, **overrides) -> dict:
    record = {
        "pmid": pmid,
        "title": f"Study {pmid}",
        "abstract": "This study reports clinical outcomes.",
        "hasAbstract": True,
        "journal": {"name": "Allowed Journal", "ta": "Allowed J", "tier": 1},
        "pubTypes": ["Journal Article"],
        "entryDate": TODAY.isoformat(),
        "score": 50,
    }
    record.update(overrides)
    return record


def _cache_record(study: dict, *, cached_hash: str | None = None) -> dict:
    return {
        "pmid": study["pmid"],
        "sourceHash": cached_hash or source_hash(study),
        "model": MODEL_ID,
        "promptVersion": PROMPT_VERSION,
        "generatedAt": "2026-08-04T12:34:56Z",
        **SUMMARY,
    }


class FakeClient:
    def __init__(self, failures: dict[str, str] | None = None):
        self.failures = failures or {}
        self.calls: list[dict] = []

    def summarize(self, study: dict) -> dict[str, str]:
        self.calls.append(study)
        message = self.failures.get(study["pmid"])
        if message is not None:
            raise CloudflareAIError(message)
        return dict(SUMMARY)


class EnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = _scratch_paths(Path(self._tmp.name))
        self.paths.config_dir.mkdir(parents=True)
        self.paths.journals_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "topicFilter": "clinical[tiab]",
                    "journals": [
                        {
                            "name": "Allowed Journal",
                            "ta": "Allowed J",
                            "specialty": "general-medicine",
                            "tier": 1,
                            "scope": "all",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_filters_window_deduplicates_and_applies_eligibility_before_ordered_calls(self) -> None:
        records = [
            _study("2", entryDate="2026-08-04", score=95, title="Newest duplicate"),
            _study("2", entryDate="2026-08-03", score=99, title="Older duplicate"),
            _study("10", entryDate="2026-08-04", score=80),
            _study("9", entryDate="2026-08-04", score=80),
            _study("1", entryDate="2026-08-05", score=1),
            _study("20", hasAbstract=False),
            _study("21", journal={"name": "Removed", "ta": "Removed J", "tier": 1}),
            _study("22", entryDate="2026-04-06"),
        ]
        client = FakeClient()

        result = enrich.run(
            window_days=120,
            paths=self.paths,
            today=TODAY,
            client=client,
            records=records,
        )

        self.assertEqual([record["pmid"] for record in client.calls], ["1", "2", "10", "9"])
        self.assertEqual(client.calls[1]["title"], "Newest duplicate")
        self.assertEqual(
            result,
            enrich.EnrichmentResult(eligible=4, cached=0, generated=4, failed=0, excluded=2),
        )

    def test_matching_cache_skips_call_but_stale_cache_is_regenerated_with_metadata(self) -> None:
        matching = _study("1")
        stale = _study("2")
        enrichment_store.write_day(
            TODAY - dt.timedelta(days=1),
            [_cache_record(matching), _cache_record(stale, cached_hash="stale-source-hash")],
            self.paths,
        )
        client = FakeClient()

        result = enrich.run(
            window_days=120,
            paths=self.paths,
            today=TODAY,
            client=client,
            records=[matching, stale],
        )

        self.assertEqual([record["pmid"] for record in client.calls], ["2"])
        self.assertEqual(
            result,
            enrich.EnrichmentResult(eligible=2, cached=1, generated=1, failed=0, excluded=0),
        )
        written = enrichment_store.read_latest(self.paths)["2"]
        self.assertEqual(
            {key: written[key] for key in ("pmid", "sourceHash", "model", "promptVersion")},
            {
                "pmid": "2",
                "sourceHash": source_hash(stale),
                "model": MODEL_ID,
                "promptVersion": PROMPT_VERSION,
            },
        )
        self.assertEqual({key: written[key] for key in SUMMARY}, SUMMARY)
        self.assertRegex(written["generatedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        generated_at = dt.datetime.fromisoformat(written["generatedAt"].replace("Z", "+00:00"))
        self.assertEqual(generated_at.tzinfo, dt.timezone.utc)

    def test_caps_calls_to_limit_without_reducing_eligible_count(self) -> None:
        client = FakeClient()
        records = [_study(str(pmid), score=pmid) for pmid in range(1, 5)]

        result = enrich.run(
            window_days=120,
            limit=2,
            paths=self.paths,
            today=TODAY,
            client=client,
            records=records,
        )

        self.assertEqual([record["pmid"] for record in client.calls], ["4", "3"])
        self.assertEqual(
            result,
            enrich.EnrichmentResult(eligible=4, cached=0, generated=2, failed=0, excluded=0),
        )

    def test_cloudflare_failure_is_warned_and_later_successes_are_persisted(self) -> None:
        client = FakeClient({"3": "non-retryable HTTP 400"})
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = enrich.run(
                window_days=120,
                paths=self.paths,
                today=TODAY,
                client=client,
                records=[_study("3", score=90), _study("2", score=80)],
            )

        self.assertEqual([record["pmid"] for record in client.calls], ["3", "2"])
        self.assertEqual(
            output.getvalue(),
            "WARNING: Chinese enrichment failed for PMID 3: non-retryable HTTP 400\n",
        )
        self.assertEqual(
            result,
            enrich.EnrichmentResult(eligible=2, cached=0, generated=1, failed=1, excluded=0),
        )
        self.assertEqual(set(enrichment_store.read_latest(self.paths)), {"2"})

    def test_reads_archive_when_records_are_not_injected(self) -> None:
        from evidence_digest import store

        store.append_archive("2026-08-05", [_study("1")], self.paths)
        store.append_archive("2026-04-06", [_study("2", entryDate="2026-04-06")], self.paths)
        client = FakeClient()

        result = enrich.run(
            window_days=120, paths=self.paths, today=TODAY, client=client
        )

        self.assertEqual([record["pmid"] for record in client.calls], ["1"])
        self.assertEqual(result.eligible, 1)

    def test_rejects_invalid_limit_and_missing_client(self) -> None:
        for limit in (0, 201):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "limit must be between 1 and 200"):
                    enrich.run(
                        window_days=120,
                        limit=limit,
                        paths=self.paths,
                        today=TODAY,
                        client=FakeClient(),
                        records=[],
                    )

        with self.assertRaisesRegex(ValueError, "client is required"):
            enrich.run(
                window_days=120,
                paths=self.paths,
                today=TODAY,
                records=[],
            )


class EnrichmentCliTests(unittest.TestCase):
    def test_missing_either_secret_skips_without_constructing_client(self) -> None:
        from evidence_digest import cli

        secret_cases = (
            {},
            {"CLOUDFLARE_ACCOUNT_ID": "account-secret"},
            {"CLOUDFLARE_AI_TOKEN": "token-secret"},
        )
        for environment in secret_cases:
            with self.subTest(environment=sorted(environment)):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch.object(cli, "CloudflareAIClient") as client_class,
                    mock.patch.object(cli.enrich_mod, "run") as run,
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = cli.main(["enrich-zh"])

                self.assertEqual(exit_code, 0)
                client_class.assert_not_called()
                run.assert_not_called()
                self.assertEqual(len(stdout.getvalue().splitlines()), 1)
                self.assertTrue(stdout.getvalue().startswith("WARNING:"))
                self.assertNotIn("account-secret", stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("token-secret", stdout.getvalue() + stderr.getvalue())

    def test_configured_cli_calls_enrichment_and_prints_only_safe_summary(self) -> None:
        from evidence_digest import cli

        account_id = "account-secret"
        token = "token-secret"
        client = object()
        result = enrich.EnrichmentResult(
            eligible=5, cached=2, generated=2, failed=1, excluded=3
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.dict(
                os.environ,
                {
                    "CLOUDFLARE_ACCOUNT_ID": account_id,
                    "CLOUDFLARE_AI_TOKEN": token,
                },
                clear=True,
            ),
            mock.patch.object(cli, "CloudflareAIClient", return_value=client) as client_class,
            mock.patch.object(cli.enrich_mod, "run", return_value=result) as run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = cli.main(["enrich-zh", "--window-days", "7", "--limit", "3"])

        self.assertEqual(exit_code, 0)
        client_class.assert_called_once_with(account_id, token)
        run.assert_called_once_with(window_days=7, limit=3, client=client)
        self.assertEqual(
            stdout.getvalue(),
            "ZH ENRICHMENT: eligible=5 cached=2 generated=2 failed=1 excluded=3\n",
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(account_id, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
