"""Pure prompt, response-validation, and cache-contract helpers for Chinese summaries."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re


MODEL_ID = "@cf/qwen/qwen3-30b-a3b-fp8"
PROMPT_VERSION = "zh-v1"
SUMMARY_FIELDS = ("objective", "methods", "results", "conclusion")

SYSTEM_PROMPT = """你是医学文献摘要助手。只能依据给定的英文标题和摘要，用简体中文总结；不得补充摘要未提供的数字、样本、机制或结论。摘要未说明的信息写“摘要未说明”。只返回一个 JSON 对象，不要 Markdown，不要解释，不要思考过程。对象必须且只能包含 objective、methods、results、conclusion 四个字符串字段。"""


class SummaryValidationError(ValueError):
    """A model response or cache record does not meet the summary contract."""


def source_hash(study: dict) -> str:
    """Hash exactly the source content and model contract that affect a summary."""
    payload = {
        "title": str(study.get("title") or ""),
        "abstract": str(study.get("abstract") or ""),
        "model": MODEL_ID,
        "promptVersion": PROMPT_VERSION,
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_messages(study: dict) -> list[dict[str, str]]:
    """Construct the fixed two-message prompt without leaking other study fields."""
    user_content = "\n".join(
        (
            f"PMID: {str(study.get('pmid') or '')}",
            f"Title: {str(study.get('title') or '')}",
            f"Abstract: {str(study.get('abstract') or '')}",
        )
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_response(text: str) -> dict[str, str]:
    """Parse and normalize a response that is exactly the required JSON object."""
    if "```" in text or "<think" in text.casefold() or "</think>" in text.casefold():
        raise SummaryValidationError("model reasoning or markdown is not allowed")
    try:
        response = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SummaryValidationError("response must be valid JSON") from exc
    if not isinstance(response, dict) or set(response) != set(SUMMARY_FIELDS):
        raise SummaryValidationError("response must contain exactly the summary fields")

    normalized: dict[str, str] = {}
    for field in SUMMARY_FIELDS:
        value = response[field]
        if not isinstance(value, str):
            raise SummaryValidationError("summary fields must be strings")
        value = value.strip()
        if not value:
            raise SummaryValidationError("summary fields must not be empty")
        if len(value) > 600:
            raise SummaryValidationError("summary fields must be at most 600 characters")
        normalized[field] = value

    if not re.search(r"[\u3400-\u9fff]", "".join(normalized.values())):
        raise SummaryValidationError("summary must contain Chinese text")
    return normalized


def _is_utc_iso_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z") or "T" not in value:
        return False
    try:
        dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def validate_cache_record(record: dict) -> bool:
    """Return whether a persisted summary is complete and structurally valid."""
    if not isinstance(record, dict):
        return False
    required = {"pmid", "sourceHash", "model", "promptVersion", "generatedAt", *SUMMARY_FIELDS}
    if not required.issubset(record):
        return False
    if not all(isinstance(record[key], str) and record[key].strip() for key in ("pmid", "sourceHash", "model", "promptVersion")):
        return False
    if not _is_utc_iso_timestamp(record["generatedAt"]):
        return False
    try:
        parse_response(json.dumps({field: record[field] for field in SUMMARY_FIELDS}, ensure_ascii=False))
    except SummaryValidationError:
        return False
    return True


def cache_matches(study: dict, record: dict) -> bool:
    """Return whether a valid cache record belongs to this study and contract."""
    return (
        validate_cache_record(record)
        and record["pmid"] == str(study.get("pmid") or "")
        and record["sourceHash"] == source_hash(study)
        and record["model"] == MODEL_ID
        and record["promptVersion"] == PROMPT_VERSION
    )
