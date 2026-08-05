"""Atom 1.0 feeds, built by hand with xml.etree.ElementTree.

Why hand-rolled: the pipeline runs stdlib-only, so no feedgen. Atom is simple
enough that this is not a hardship - one <feed> per topic plus one combined
`all.xml`, each entry keyed by a stable `urn:pmid:<pmid>` id so a reader's feed
client never sees a duplicate even if a study's card is later re-served with a
different score after a rebuild.
"""

from __future__ import annotations

import datetime as dt
import html
import xml.etree.ElementTree as ET
from pathlib import Path

from evidence_digest.config import Taxonomy

ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("", ATOM_NS)


def _tag(name: str) -> str:
    return f"{{{ATOM_NS}}}{name}"


def _sub_text(parent: ET.Element, name: str, text: str) -> ET.Element:
    el = ET.SubElement(parent, _tag(name))
    el.text = text
    return el


def _rfc3339_from_date(date_str: str) -> str:
    """entryDate is always YYYY-MM-DD; Atom wants a full timestamp."""
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{date_str}T00:00:00Z"


def _build_feed_xml(
    *,
    title: str,
    feed_id: str,
    self_href: str,
    alternate_href: str,
    studies: list[dict],
    generated_at: str,
) -> ET.ElementTree:
    feed = ET.Element(_tag("feed"))
    _sub_text(feed, "title", title)
    _sub_text(feed, "id", feed_id)
    _sub_text(feed, "updated", generated_at)

    self_link = ET.SubElement(feed, _tag("link"))
    self_link.set("rel", "self")
    self_link.set("href", self_href)

    if alternate_href:
        alt_link = ET.SubElement(feed, _tag("link"))
        alt_link.set("rel", "alternate")
        alt_link.set("href", alternate_href)

    author = ET.SubElement(feed, _tag("author"))
    _sub_text(author, "name", "Evidence Digest")

    for study in studies:
        entry = ET.SubElement(feed, _tag("entry"))
        _sub_text(entry, "title", study["title"])
        _sub_text(entry, "id", f"urn:pmid:{study['pmid']}")
        timestamp = _rfc3339_from_date(study["entryDate"])
        _sub_text(entry, "updated", timestamp)
        _sub_text(entry, "published", timestamp)

        link = ET.SubElement(entry, _tag("link"))
        link.set("rel", "alternate")
        link.set("href", study["url"])

        _sub_text(entry, "summary", study.get("takeaway") or "")

        entry_author = ET.SubElement(entry, _tag("author"))
        _sub_text(entry_author, "name", study.get("authorLine") or study["journal"]["name"])

        for topic_slug in study.get("topics", []):
            category = ET.SubElement(entry, _tag("category"))
            category.set("term", topic_slug)

    tree = ET.ElementTree(feed)
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass  # ET.indent needs Python 3.9+; the feed is still valid without it.
    return tree


def write_feed(
    path: Path,
    *,
    title: str,
    feed_id: str,
    site_url: str,
    self_path: str,
    alternate_path: str,
    studies: list[dict],
    generated_at: str,
) -> None:
    base = site_url.rstrip("/") if site_url else ""
    self_href = f"{base}{self_path}"
    alternate_href = f"{base}{alternate_path}" if base else ""

    tree = _build_feed_xml(
        title=title,
        feed_id=feed_id,
        self_href=self_href,
        alternate_href=alternate_href,
        studies=studies,
        generated_at=generated_at,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(path), encoding="utf-8", xml_declaration=True)


def write_chinese_feed(
    path: Path,
    *,
    studies: list[dict],
    summaries: dict[str, dict],
    site_url: str,
    generated_at: str,
) -> None:
    """Write the filtered Chinese-summary Atom feed in the supplied order."""
    base = site_url.rstrip("/") if site_url else ""
    feed = ET.Element(_tag("feed"))
    _sub_text(feed, "title", "Evidence Digest — 中文精选")
    _sub_text(feed, "id", "urn:evidence-digest:zh")
    _sub_text(feed, "updated", generated_at)

    self_link = ET.SubElement(feed, _tag("link"))
    self_link.set("rel", "self")
    self_link.set("href", f"{base}/feeds/selected-journals-zh.xml")

    if base:
        alternate_link = ET.SubElement(feed, _tag("link"))
        alternate_link.set("rel", "alternate")
        alternate_link.set("href", base)

    author = ET.SubElement(feed, _tag("author"))
    _sub_text(author, "name", "Evidence Digest")

    seen_pmids: set[str] = set()
    fields = (
        ("研究目的：", "objective"),
        ("研究方法：", "methods"),
        ("主要结果：", "results"),
        ("结论：", "conclusion"),
    )
    for study in studies:
        pmid = str(study.get("pmid") or "")
        summary = summaries.get(pmid)
        if not summary or pmid in seen_pmids:
            continue
        seen_pmids.add(pmid)

        entry = ET.SubElement(feed, _tag("entry"))
        journal_ta = str((study.get("journal") or {}).get("ta") or "Unknown journal")
        entry_title = f"【{journal_ta}】{study['title']}"
        _sub_text(entry, "title", entry_title)
        _sub_text(entry, "id", f"urn:evidence-digest:zh:{pmid}")
        _sub_text(entry, "updated", str(summary["generatedAt"]))
        _sub_text(entry, "published", _rfc3339_from_date(study["entryDate"]))

        url = str(study["url"])
        link = ET.SubElement(entry, _tag("link"))
        link.set("rel", "alternate")
        link.set("href", url)

        plain_lines = [f"{label}{str(summary[key])}" for label, key in fields]
        plain_lines.append(f"PubMed：{url}")
        _sub_text(entry, "summary", "\n".join(plain_lines))

        escaped_title = html.escape(entry_title, quote=True)
        escaped_url = html.escape(url, quote=True)
        html_parts = [f"<h2>{escaped_title}</h2>"]
        html_parts.extend(
            f"<p><strong>{label}</strong>{html.escape(str(summary[key]), quote=True)}</p>"
            for label, key in fields
        )
        html_parts.append(f'<p><a href="{escaped_url}">PubMed</a></p>')
        content = ET.SubElement(entry, _tag("content"))
        content.set("type", "html")
        content.text = "".join(html_parts)

        entry_author = ET.SubElement(entry, _tag("author"))
        _sub_text(
            entry_author,
            "name",
            str(study.get("authorLine") or (study.get("journal") or {}).get("name") or journal_ta),
        )

        for topic_slug in study.get("topics", []):
            category = ET.SubElement(entry, _tag("category"))
            category.set("term", str(topic_slug))

    tree = ET.ElementTree(feed)
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(path), encoding="utf-8", xml_declaration=True)


def write_all_feeds(
    *,
    taxonomy: Taxonomy,
    studies_by_topic: dict[str, list[dict]],
    all_studies: list[dict],
    site_url: str,
    feeds_dir: Path,
    generated_at: str,
) -> int:
    """Write one feed per topic plus feeds/all.xml. Returns the number of
    feed files written."""
    count = 0
    for topic in taxonomy.all_topics:
        write_feed(
            feeds_dir / f"{topic.slug}.xml",
            title=f"Evidence Digest — {topic.name}",
            feed_id=f"urn:evidence-digest:topic:{topic.slug}",
            site_url=site_url,
            self_path=f"/feeds/{topic.slug}.xml",
            alternate_path=f"/topics/{topic.slug}",
            studies=studies_by_topic.get(topic.slug, []),
            generated_at=generated_at,
        )
        count += 1

    write_feed(
        feeds_dir / "all.xml",
        title="Evidence Digest — All specialties",
        feed_id="urn:evidence-digest:all",
        site_url=site_url,
        self_path="/feeds/all.xml",
        alternate_path="/",
        studies=all_studies,
        generated_at=generated_at,
    )
    count += 1
    return count
