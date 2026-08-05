from __future__ import annotations

from dataclasses import dataclass
import re


EXCLUDED_PUB_TYPES = frozenset({
    "news",
    "editorial",
    "comment",
    "letter",
    "published erratum",
    "retraction of publication",
    "retracted publication",
})

_EXCLUDED_TITLE_PATTERNS = (
    ("correction", re.compile(r"^(?:author\s+|publisher\s+)?correction\s*:", re.I)),
    ("erratum", re.compile(r"^erratum\s*:", re.I)),
    ("retraction", re.compile(r"^retraction(?:\s+note)?\s*:", re.I)),
    ("reviewer-highlight", re.compile(r"\breviewer\s+highlight\b", re.I)),
    ("announcement", re.compile(r"^(?:journal\s+)?announcement\s*:", re.I)),
    ("journal-announcement", re.compile(r"\bjournal\s+announcement\b", re.I)),
    ("editorial-board", re.compile(r"\beditorial\s+board\b", re.I)),
    ("issue-highlights", re.compile(r"\bissue\s+highlights?\b", re.I)),
    ("table-of-contents", re.compile(r"\btable\s+of\s+contents\b", re.I)),
    ("periodical-news", re.compile(
        r"^[^:]{2,120}:\s*news\s+(?:january|february|march|april|may|june|"
        r"july|august|september|october|november|december|20\d{2})\b",
        re.I,
    )),
)


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str


def assess(study: dict, allowed_journal_tas: set[str]) -> EligibilityDecision:
    """Return a deterministic inclusion decision without network or AI."""
    abstract = str(study.get("abstract") or "").strip()
    if not study.get("hasAbstract") or not abstract:
        return EligibilityDecision(False, "missing-abstract")

    journal_ta = str((study.get("journal") or {}).get("ta") or "")
    if journal_ta not in allowed_journal_tas:
        return EligibilityDecision(False, "journal-not-configured")

    for pub_type in study.get("pubTypes") or []:
        normalized = str(pub_type).strip().casefold()
        if normalized in EXCLUDED_PUB_TYPES:
            return EligibilityDecision(False, f"excluded-pub-type:{normalized}")

    title = str(study.get("title") or "").strip()
    for reason, pattern in _EXCLUDED_TITLE_PATTERNS:
        if pattern.search(title):
            return EligibilityDecision(False, f"excluded-title:{reason}")

    return EligibilityDecision(True, "eligible")
