"""feeds.py tests: hand-built Atom XML parses back cleanly with ElementTree
and carries the fields the contract promises (stable urn:pmid ids, RFC-3339
timestamps, self link, summary=takeaway, per-topic categories)."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_PIPELINE_DIR = str(_Path(__file__).resolve().parent.parent)
if _PIPELINE_DIR not in _sys.path:
    _sys.path.insert(0, _PIPELINE_DIR)

import datetime as dt
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from evidence_digest import feeds
from evidence_digest.config import Specialty, Taxonomy, Topic

ATOM = "http://www.w3.org/2005/Atom"
NS = {"a": ATOM}


def _topic(slug: str) -> Topic:
    return Topic(
        slug=slug, name=slug.title(), blurb="", catch_all=False,
        mesh=(), phrases=(), acronyms=(), veto=(),
        specialty_slug="heme", specialty_order=10, order_in_specialty=0,
    )


def _taxonomy() -> Taxonomy:
    topic = _topic("heme-aml")
    specialty = Specialty(slug="heme", order=10, name="Heme", icon="x", blurb="", topics=(topic,))
    return Taxonomy(specialties=(specialty,))


def _card(pmid: str, entry_date: str = "2026-07-24") -> dict:
    return {
        "pmid": pmid, "title": f"Study {pmid}", "takeaway": f"Takeaway for {pmid}.",
        "authorLine": "Smith J, et al", "journal": {"name": "Blood", "ta": "Blood", "tier": 1},
        "topics": ["heme-aml"], "entryDate": entry_date,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


def _zh_summary(pmid: str) -> dict:
    return {
        "pmid": pmid,
        "generatedAt": "2026-07-24T11:22:33Z",
        "objective": "比较 A&B 与 <标准治疗>。",
        "methods": "开展随机试验。",
        "results": "A&B 改善结局。",
        "conclusion": "值得进一步研究。",
    }


class WriteFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_feed_parses_back_with_elementtree(self) -> None:
        path = self.dir / "heme-aml.xml"
        feeds.write_feed(
            path, title="Evidence Digest — Heme AML", feed_id="urn:evidence-digest:topic:heme-aml",
            site_url="https://example.org", self_path="/feeds/heme-aml.xml",
            alternate_path="/topics/heme-aml", studies=[_card("1"), _card("2")],
            generated_at="2026-07-24T12:00:00Z",
        )
        tree = ET.parse(path)  # must not raise
        root = tree.getroot()
        self.assertEqual(root.tag, f"{{{ATOM}}}feed")

    def test_entry_ids_are_stable_urn_pmid(self) -> None:
        path = self.dir / "f.xml"
        feeds.write_feed(
            path, title="T", feed_id="urn:x", site_url="", self_path="/feeds/f.xml",
            alternate_path="", studies=[_card("12345678")], generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(path).getroot()
        entry = root.find("a:entry", NS)
        self.assertEqual(entry.find("a:id", NS).text, "urn:pmid:12345678")

    def test_summary_is_the_takeaway(self) -> None:
        path = self.dir / "f.xml"
        feeds.write_feed(
            path, title="T", feed_id="urn:x", site_url="", self_path="/feeds/f.xml",
            alternate_path="", studies=[_card("1")], generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(path).getroot()
        entry = root.find("a:entry", NS)
        self.assertEqual(entry.find("a:summary", NS).text, "Takeaway for 1.")

    def test_updated_is_rfc3339(self) -> None:
        path = self.dir / "f.xml"
        feeds.write_feed(
            path, title="T", feed_id="urn:x", site_url="", self_path="/feeds/f.xml",
            alternate_path="", studies=[_card("1", "2026-01-05")], generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(path).getroot()
        entry = root.find("a:entry", NS)
        updated = entry.find("a:updated", NS).text
        self.assertEqual(updated, "2026-01-05T00:00:00Z")
        # Must be a real, parseable RFC-3339-ish timestamp.
        dt.datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")

    def test_self_link_uses_site_url(self) -> None:
        path = self.dir / "f.xml"
        feeds.write_feed(
            path, title="T", feed_id="urn:x", site_url="https://digest.example",
            self_path="/feeds/heme-aml.xml", alternate_path="/topics/heme-aml",
            studies=[], generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(path).getroot()
        links = root.findall("a:link", NS)
        self_links = [l for l in links if l.get("rel") == "self"]
        self.assertEqual(self_links[0].get("href"), "https://digest.example/feeds/heme-aml.xml")

    def test_category_per_topic(self) -> None:
        path = self.dir / "f.xml"
        card = _card("1")
        card["topics"] = ["heme-aml", "heme-other"]
        feeds.write_feed(
            path, title="T", feed_id="urn:x", site_url="", self_path="/feeds/f.xml",
            alternate_path="", studies=[card], generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(path).getroot()
        entry = root.find("a:entry", NS)
        categories = {c.get("term") for c in entry.findall("a:category", NS)}
        self.assertEqual(categories, {"heme-aml", "heme-other"})

    def test_empty_studies_produces_valid_feed_with_no_entries(self) -> None:
        path = self.dir / "f.xml"
        feeds.write_feed(
            path, title="T", feed_id="urn:x", site_url="", self_path="/feeds/f.xml",
            alternate_path="", studies=[], generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(path).getroot()
        self.assertEqual(root.findall("a:entry", NS), [])

    def test_special_characters_are_escaped(self) -> None:
        path = self.dir / "f.xml"
        card = _card("1")
        card["title"] = 'Risk & benefit of <drug> "X" in >50s'
        feeds.write_feed(
            path, title="T", feed_id="urn:x", site_url="", self_path="/feeds/f.xml",
            alternate_path="", studies=[card], generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(path).getroot()  # would raise ParseError if unescaped
        entry_title = root.find("a:entry/a:title", NS).text
        self.assertEqual(entry_title, 'Risk & benefit of <drug> "X" in >50s')

    def test_existing_english_feed_bytes_are_unchanged(self) -> None:
        path = self.dir / "f.xml"
        card = _card("1")
        card.update({
            "title": 'Risk & benefit of <drug> "X" in >50s',
            "takeaway": "Takeaway & context <now>.",
            "journal": {"name": "Blood & Marrow", "ta": "Blood", "tier": 1},
            "url": "https://pubmed.ncbi.nlm.nih.gov/1/?a=1&b=2",
        })
        feeds.write_feed(
            path, title="Evidence Digest — Heme & AML", feed_id="urn:test:english",
            site_url="https://example.org", self_path="/feeds/f.xml",
            alternate_path="/topics/heme-aml", studies=[card],
            generated_at="2026-07-24T12:00:00Z",
        )
        expected = """<?xml version='1.0' encoding='utf-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Evidence Digest — Heme &amp; AML</title>
  <id>urn:test:english</id>
  <updated>2026-07-24T12:00:00Z</updated>
  <link rel="self" href="https://example.org/feeds/f.xml" />
  <link rel="alternate" href="https://example.org/topics/heme-aml" />
  <author>
    <name>Evidence Digest</name>
  </author>
  <entry>
    <title>Risk &amp; benefit of &lt;drug&gt; "X" in &gt;50s</title>
    <id>urn:pmid:1</id>
    <updated>2026-07-24T00:00:00Z</updated>
    <published>2026-07-24T00:00:00Z</published>
    <link rel="alternate" href="https://pubmed.ncbi.nlm.nih.gov/1/?a=1&amp;b=2" />
    <summary>Takeaway &amp; context &lt;now&gt;.</summary>
    <author>
      <name>Smith J, et al</name>
    </author>
    <category term="heme-aml" />
  </entry>
</feed>""".encode("utf-8")
        self.assertEqual(path.read_bytes(), expected)


class WriteChineseFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "selected-journals-zh.xml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_serializes_chinese_summary_as_atom_text_and_escaped_html(self) -> None:
        study = _card("12345678")
        study.update({
            "title": "English & escaped title",
            "journal": {"name": "Nature Immunology", "ta": "Nat Immunol", "tier": 1},
            "topics": ["immune-autoimmune", "immune-other"],
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/?a=1&b=2",
        })
        feeds.write_chinese_feed(
            self.path,
            studies=[study],
            summaries={"12345678": _zh_summary("12345678")},
            site_url="https://example.org",
            generated_at="2026-07-24T12:00:00Z",
        )

        root = ET.parse(self.path).getroot()
        self.assertEqual(root.find("a:title", NS).text, "Evidence Digest — 中文精选")
        entry = root.find("a:entry", NS)
        self.assertEqual(entry.find("a:title", NS).text, "【Nat Immunol】English & escaped title")
        self.assertEqual(entry.find("a:id", NS).text, "urn:evidence-digest:zh:12345678")
        self.assertEqual(entry.find("a:updated", NS).text, "2026-07-24T11:22:33Z")
        self.assertEqual(entry.find("a:published", NS).text, "2026-07-24T00:00:00Z")

        plain = entry.find("a:summary", NS).text
        for label in ("研究目的：", "研究方法：", "主要结果：", "结论："):
            self.assertIn(label, plain)
        self.assertIn("比较 A&B 与 <标准治疗>。", plain)
        self.assertIn(study["url"], plain)

        content = entry.find("a:content", NS)
        self.assertEqual(content.get("type"), "html")
        for label in ("研究目的：", "研究方法：", "主要结果：", "结论："):
            self.assertIn(label, content.text)
        self.assertIn("比较 A&amp;B 与 &lt;标准治疗&gt;。", content.text)
        self.assertIn(
            '<a href="https://pubmed.ncbi.nlm.nih.gov/12345678/?a=1&amp;b=2">PubMed</a>',
            content.text,
        )
        categories = [category.get("term") for category in entry.findall("a:category", NS)]
        self.assertEqual(categories, ["immune-autoimmune", "immune-other"])

    def test_skips_missing_summaries_and_duplicate_pmids(self) -> None:
        first = _card("12345678")
        duplicate = dict(first)
        missing = _card("87654321")
        feeds.write_chinese_feed(
            self.path,
            studies=[first, duplicate, missing],
            summaries={"12345678": _zh_summary("12345678")},
            site_url="",
            generated_at="2026-07-24T12:00:00Z",
        )
        root = ET.parse(self.path).getroot()
        entries = root.findall("a:entry", NS)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].find("a:id", NS).text, "urn:evidence-digest:zh:12345678")


class WriteAllFeedsTests(unittest.TestCase):
    def test_writes_one_file_per_topic_plus_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feeds_dir = Path(tmp)
            count = feeds.write_all_feeds(
                taxonomy=_taxonomy(),
                studies_by_topic={"heme-aml": [_card("1")]},
                all_studies=[_card("1")],
                site_url="https://example.org",
                feeds_dir=feeds_dir,
                generated_at="2026-07-24T12:00:00Z",
            )
            self.assertEqual(count, 2)
            self.assertTrue((feeds_dir / "heme-aml.xml").exists())
            self.assertTrue((feeds_dir / "all.xml").exists())


if __name__ == "__main__":
    unittest.main()
