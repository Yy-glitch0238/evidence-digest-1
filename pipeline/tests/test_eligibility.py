from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_PIPELINE_DIR = str(_Path(__file__).resolve().parent.parent)
if _PIPELINE_DIR not in _sys.path:
    _sys.path.insert(0, _PIPELINE_DIR)

import unittest

from evidence_digest.eligibility import assess


ALLOWED_JOURNALS = {"Permitted Journal"}


def _study(**overrides) -> dict:
    record = {
        "abstract": "This study reports durable clinical outcomes.",
        "hasAbstract": True,
        "journal": {"ta": "Permitted Journal"},
        "pubTypes": ["Journal Article"],
        "title": "Durable clinical outcomes after treatment",
    }
    record.update(overrides)
    return record


class EligibilityTests(unittest.TestCase):
    def assert_excluded(self, study: dict, reason: str) -> None:
        decision = assess(study, ALLOWED_JOURNALS)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, reason)

    def test_excludes_record_without_abstract_flag(self) -> None:
        self.assert_excluded(_study(hasAbstract=False), "missing-abstract")

    def test_excludes_whitespace_only_abstract(self) -> None:
        self.assert_excluded(_study(abstract="  \n\t "), "missing-abstract")

    def test_excludes_journal_not_in_allowed_set(self) -> None:
        self.assert_excluded(
            _study(journal={"ta": "Unconfigured Journal"}), "journal-not-configured"
        )

    def test_excludes_news_pub_type(self) -> None:
        self.assert_excluded(_study(pubTypes=["News"]), "excluded-pub-type:news")

    def test_excludes_editorial_pub_type(self) -> None:
        self.assert_excluded(_study(pubTypes=["Editorial"]), "excluded-pub-type:editorial")

    def test_excludes_comment_pub_type(self) -> None:
        self.assert_excluded(_study(pubTypes=["Comment"]), "excluded-pub-type:comment")

    def test_excludes_letter_pub_type(self) -> None:
        self.assert_excluded(_study(pubTypes=["Letter"]), "excluded-pub-type:letter")

    def test_excludes_published_erratum_pub_type(self) -> None:
        self.assert_excluded(
            _study(pubTypes=["Published Erratum"]), "excluded-pub-type:published erratum"
        )

    def test_excludes_retraction_of_publication_pub_type(self) -> None:
        self.assert_excluded(
            _study(pubTypes=["Retraction of Publication"]),
            "excluded-pub-type:retraction of publication",
        )

    def test_excludes_retracted_publication_pub_type(self) -> None:
        self.assert_excluded(
            _study(pubTypes=["Retracted Publication"]), "excluded-pub-type:retracted publication"
        )

    def test_excludes_correction_title_prefix(self) -> None:
        self.assert_excluded(_study(title="Correction: dosage update"), "excluded-title:correction")

    def test_excludes_author_correction_title_prefix(self) -> None:
        self.assert_excluded(
            _study(title="Author Correction: dosage update"), "excluded-title:correction"
        )

    def test_excludes_publisher_correction_title_prefix(self) -> None:
        self.assert_excluded(
            _study(title="Publisher Correction: dosage update"), "excluded-title:correction"
        )

    def test_excludes_erratum_title_prefix(self) -> None:
        self.assert_excluded(_study(title="Erratum: dosage update"), "excluded-title:erratum")

    def test_excludes_retraction_title_prefix(self) -> None:
        self.assert_excluded(
            _study(title="Retraction: dosage update"), "excluded-title:retraction"
        )

    def test_excludes_retraction_note_title_prefix(self) -> None:
        self.assert_excluded(
            _study(title="Retraction Note: dosage update"), "excluded-title:retraction"
        )

    def test_excludes_reviewer_highlight_title(self) -> None:
        self.assert_excluded(
            _study(title="A Reviewer Highlight from this issue"), "excluded-title:reviewer-highlight"
        )

    def test_excludes_announcement_title(self) -> None:
        self.assert_excluded(_study(title="Announcement: new policy"), "excluded-title:announcement")

    def test_excludes_journal_announcement_title(self) -> None:
        self.assert_excluded(
            _study(title="Journal Announcement: new policy"), "excluded-title:announcement"
        )

    def test_excludes_editorial_board_title(self) -> None:
        self.assert_excluded(
            _study(title="Meet the Editorial Board"), "excluded-title:editorial-board"
        )

    def test_excludes_issue_highlights_title(self) -> None:
        self.assert_excluded(
            _study(title="Issue Highlights for August"), "excluded-title:issue-highlights"
        )

    def test_excludes_table_of_contents_title(self) -> None:
        self.assert_excluded(
            _study(title="Table of Contents"), "excluded-title:table-of-contents"
        )

    def test_excludes_monthly_journal_news_title(self) -> None:
        self.assert_excluded(
            _study(title="Human vaccines and immunotherapeutics: News June 2025."),
            "excluded-title:periodical-news",
        )

    def test_retains_research_title_containing_news(self) -> None:
        decision = assess(
            _study(title="Good news for vaccine durability after booster dosing"), ALLOWED_JOURNALS
        )
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.reason, "eligible")

    def test_retains_reviews(self) -> None:
        self.assertTrue(assess(_study(pubTypes=["Review"]), ALLOWED_JOURNALS).eligible)

    def test_retains_randomized_trials(self) -> None:
        self.assertTrue(
            assess(_study(pubTypes=["Randomized Controlled Trial"]), ALLOWED_JOURNALS).eligible
        )

    def test_retains_observational_studies(self) -> None:
        self.assertTrue(assess(_study(pubTypes=["Journal Article"]), ALLOWED_JOURNALS).eligible)

    def test_retains_basic_research(self) -> None:
        self.assertTrue(assess(_study(pubTypes=["In Vitro"]), ALLOWED_JOURNALS).eligible)

    def test_retains_case_reports(self) -> None:
        self.assertTrue(assess(_study(pubTypes=["Case Reports"]), ALLOWED_JOURNALS).eligible)


if __name__ == "__main__":
    unittest.main()
