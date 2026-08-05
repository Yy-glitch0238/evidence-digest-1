import hashlib
import json
import unittest

from evidence_digest.zh_summary import (
    MODEL_ID,
    PROMPT_VERSION,
    SUMMARY_FIELDS,
    SummaryValidationError,
    build_messages,
    cache_matches,
    parse_response,
    source_hash,
    validate_cache_record,
)


STUDY = {
    "pmid": "12345678",
    "title": "A controlled trial",
    "abstract": "A concise English abstract.",
    "unrelated": "must never enter the prompt or hash",
}
VALID_SUMMARY = {
    "objective": "评估治疗效果",
    "methods": "进行随机对照试验",
    "results": "治疗组结局改善",
    "conclusion": "治疗可能有效",
}


class SourceHashTests(unittest.TestCase):
    def test_is_stable_for_same_source_model_and_prompt(self):
        expected_payload = {
            "title": STUDY["title"],
            "abstract": STUDY["abstract"],
            "model": MODEL_ID,
            "promptVersion": PROMPT_VERSION,
        }
        expected = hashlib.sha256(
            json.dumps(expected_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        self.assertEqual(source_hash(STUDY), expected)
        self.assertEqual(source_hash(STUDY), source_hash(dict(STUDY)))

    def test_changes_when_title_or_abstract_changes(self):
        self.assertNotEqual(source_hash(STUDY), source_hash({**STUDY, "title": "Another trial"}))
        self.assertNotEqual(source_hash(STUDY), source_hash({**STUDY, "abstract": "Another abstract."}))

    def test_includes_model_and_prompt_version(self):
        payload = {
            "title": STUDY["title"],
            "abstract": STUDY["abstract"],
            "model": "another-model",
            "promptVersion": "another-prompt",
        }
        alternative = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(source_hash(STUDY), alternative)


class PromptTests(unittest.TestCase):
    def test_contains_only_pmid_title_and_abstract_from_study(self):
        messages = build_messages(STUDY)

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn(STUDY["pmid"], messages[1]["content"])
        self.assertIn(STUDY["title"], messages[1]["content"])
        self.assertIn(STUDY["abstract"], messages[1]["content"])
        self.assertNotIn(STUDY["unrelated"], messages[1]["content"])


class ResponseValidationTests(unittest.TestCase):
    def test_accepts_exactly_four_nonempty_chinese_strings(self):
        parsed = parse_response(json.dumps(VALID_SUMMARY, ensure_ascii=False))

        self.assertEqual(parsed, VALID_SUMMARY)

    def test_rejects_invalid_response_shapes_and_content(self):
        invalid = [
            {key: value for key, value in VALID_SUMMARY.items() if key != "results"},
            {**VALID_SUMMARY, "extra": "额外字段"},
            {**VALID_SUMMARY, "results": "  "},
            {field: "English only" for field in SUMMARY_FIELDS},
            "```json\n" + json.dumps(VALID_SUMMARY, ensure_ascii=False) + "\n```",
            "<think>reasoning</think>" + json.dumps(VALID_SUMMARY, ensure_ascii=False),
            {**VALID_SUMMARY, "results": "中" * 601},
        ]

        for response in invalid:
            with self.subTest(response=response):
                text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
                with self.assertRaises(SummaryValidationError):
                    parse_response(text)


class CacheRecordTests(unittest.TestCase):
    def _record(self):
        return {
            "pmid": STUDY["pmid"],
            "sourceHash": source_hash(STUDY),
            "model": MODEL_ID,
            "promptVersion": PROMPT_VERSION,
            "generatedAt": "2026-08-05T12:34:56Z",
            **VALID_SUMMARY,
        }

    def test_requires_metadata_fields_summary_fields_and_utc_timestamp(self):
        record = self._record()
        self.assertTrue(validate_cache_record(record))

        for key in ("pmid", "sourceHash", "model", "promptVersion", *SUMMARY_FIELDS):
            with self.subTest(missing=key):
                invalid = dict(record)
                del invalid[key]
                self.assertFalse(validate_cache_record(invalid))
        self.assertFalse(validate_cache_record({**record, "generatedAt": "2026-08-05T12:34:56+00:00"}))
        self.assertFalse(validate_cache_record({**record, "generatedAt": "2026-08-05Z"}))
        self.assertFalse(validate_cache_record({**record, "pmid": "not-a-pmid"}))

    def test_matches_only_current_source_model_and_prompt_version(self):
        record = self._record()
        self.assertTrue(cache_matches(STUDY, record))
        self.assertFalse(cache_matches(STUDY, {**record, "sourceHash": "stale"}))
        self.assertFalse(cache_matches(STUDY, {**record, "model": "other-model"}))
        self.assertFalse(cache_matches(STUDY, {**record, "promptVersion": "other-version"}))


if __name__ == "__main__":
    unittest.main()
