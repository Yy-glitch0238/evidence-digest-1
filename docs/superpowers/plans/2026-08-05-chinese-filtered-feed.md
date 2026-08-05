# Evidence Digest Chinese Filtered Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, research-only Chinese Atom feed to `Yy-glitch0238/evidence-digest-1`, enriched through Cloudflare Workers AI, scheduled every Sunday at 23:00 Beijing time and runnable manually at any time.

**Architecture:** Keep the PubMed archive and all existing English outputs unchanged. Add a pure eligibility filter, a validated Chinese-summary contract, a small stdlib-only Cloudflare client, and a deterministic gzip cache under `data/enrichment/zh/`. The existing build command joins eligible archive records to valid cached summaries and emits one additional Atom file; GitHub Actions performs harvest, enrichment, commit, build, and deployment in that order.

**Tech Stack:** Python 3.11 standard library (`unittest`, `urllib`, `json`, `gzip`, `hashlib`, `xml.etree.ElementTree`), GitHub Actions, Cloudflare Workers AI REST API, Atom XML, existing Vite/React build.

## Global Constraints

- Execute this plan in a real checkout of `Yy-glitch0238/evidence-digest-1`; the planning workspace contains documentation only.
- Apply TDD for every behavior: add one focused failing test, run it and observe the expected failure, implement the smallest change, then rerun.
- Do not change archive records, `contract/study.schema.json`, existing English Feed IDs, English Feed bodies, or the existing web API shape.
- Eligibility is deterministic. AI is never allowed to decide whether an article is included.
- The new feed path is exactly `data/feeds/selected-journals-zh.xml`, deployed as `/feeds/selected-journals-zh.xml`.
- Use the model ID `@cf/qwen/qwen3-30b-a3b-fp8`, prompt version `zh-v1`, maximum 200 new AI calls per run, and maximum 200 entries in the Chinese feed.
- A request gets one initial attempt and at most two retries. Per-article AI failure is soft and must not fail harvest, build, or deployment.
- Never print either Cloudflare secret or a complete model response.
- Use `journal.ta` as the displayed MEDLINE abbreviation; do not maintain a separate abbreviation table.
- Treat the Cloudflare endpoint and response shape as an external contract. The client endpoint is `POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/qwen/qwen3-30b-a3b-fp8`, with the text result read from `result.response`.

---

## File and Responsibility Map

- Create `pipeline/evidence_digest/eligibility.py`: pure inclusion/exclusion decision and stable reason codes.
- Create `pipeline/evidence_digest/zh_summary.py`: prompt, source hash, response validation, and cache-record validation.
- Create `pipeline/evidence_digest/cloudflare_ai.py`: authenticated REST call, bounded retry, timeout, response-envelope parsing.
- Create `pipeline/evidence_digest/enrichment_store.py`: deterministic dated gzip JSONL cache, tolerant record reads, latest-valid lookup.
- Create `pipeline/evidence_digest/enrich.py`: selection, cache reuse, ordered per-record enrichment, soft-failure accounting.
- Modify `pipeline/evidence_digest/cli.py`: add `enrich-zh` command and secret-aware skip behavior.
- Modify `pipeline/evidence_digest/feeds.py`: add the Chinese Atom serializer without altering the English serializer.
- Modify `pipeline/evidence_digest/build.py`: join valid cached summaries to eligible archive records and write the new feed.
- Add focused tests in `pipeline/tests/test_eligibility.py`, `test_zh_summary.py`, `test_cloudflare_ai.py`, `test_enrichment_store.py`, and `test_enrich.py`; extend `test_feeds.py` and `test_build.py`.
- Modify `.github/workflows/harvest.yml`: weekly cron, 8-day defaults, enrichment step, cache commit.
- Modify `.github/workflows/pages.yml`: rebuild Pages when committed enrichment changes.
- Modify `README.md`: secrets, schedule, manual run, new feed URL, and expected first-run behavior.

---

### Task 0: Prepare an isolated implementation checkout

**Files:**

- Verify only.

- [ ] **Step 1: Confirm this is the real fork checkout**

```bash
git rev-parse --show-toplevel
git remote -v
git status --short
```

Expected: the repository contains `pipeline/`, `web/`, `worker/`, and `.github/`; `origin` points to `Yy-glitch0238/evidence-digest-1`; the working tree has no unrelated changes. Stop before implementation if any of these conditions is false.

- [ ] **Step 2: Isolate the feature work**

Use the required `superpowers:using-git-worktrees` workflow when the execution environment supports worktrees. Otherwise create the feature branch before making any code commit:

```bash
git switch -c feat/chinese-filtered-feed
git branch --show-current
```

Expected: current branch is `feat/chinese-filtered-feed`.

---

### Task 1: Add the deterministic eligibility gate

**Files:**

- Create: `pipeline/evidence_digest/eligibility.py`
- Test: `pipeline/tests/test_eligibility.py`

**Interface:**

```python
@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str

def assess(study: dict, allowed_journal_tas: set[str]) -> EligibilityDecision:
    """Return a deterministic inclusion decision without network or AI."""
```

- [ ] **Step 1: Write the failing tests for every exclusion path and every retained study class**

Create `pipeline/tests/test_eligibility.py` with a `_study()` factory and separate tests for:

- `hasAbstract=False`;
- empty/whitespace `abstract` even when `hasAbstract=True`;
- journal not in `allowed_journal_tas`;
- each excluded `pubTypes` value: `News`, `Editorial`, `Comment`, `Letter`, `Published Erratum`, `Retraction of Publication`, `Retracted Publication`;
- title prefixes `Correction:`, `Author Correction:`, `Publisher Correction:`, `Erratum:`, `Retraction:`, `Retraction Note:`;
- `Reviewer Highlight`, `Announcement:`, `Journal Announcement`, `Editorial Board`, `Issue Highlights`, and `Table of Contents`;
- monthly/annual journal-news titles such as `Human vaccines and immunotherapeutics: News June 2025.`;
- ordinary research title `Good news for vaccine durability after booster dosing` remains eligible;
- reviews, randomized trials, observational studies, basic research, and case reports remain eligible.

Use the canonical study fields `abstract`, `hasAbstract`, `journal.ta`, `pubTypes`, and `title`.

- [ ] **Step 2: Run the eligibility tests and confirm the module-not-found failure**

Run:

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_eligibility -v
```

Expected: `ModuleNotFoundError: No module named 'evidence_digest.eligibility'`.

- [ ] **Step 3: Implement the smallest deterministic filter**

Create `pipeline/evidence_digest/eligibility.py` with:

```python
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
```

- [ ] **Step 4: Run the focused tests and the existing classifier/score tests**

Run:

```bash
PYTHONPATH=pipeline python -m unittest \
  pipeline.tests.test_eligibility \
  pipeline.tests.test_classify \
  pipeline.tests.test_score -v
```

Expected: all tests pass; the normal-title `news` regression test remains green.

- [ ] **Step 5: Commit the eligibility gate**

```bash
git add pipeline/evidence_digest/eligibility.py pipeline/tests/test_eligibility.py
git commit -m "feat: filter non-research Chinese feed records"
```

---

### Task 2: Define and validate the Chinese summary contract

**Files:**

- Create: `pipeline/evidence_digest/zh_summary.py`
- Test: `pipeline/tests/test_zh_summary.py`

**Interface:**

Constants: `MODEL_ID = "@cf/qwen/qwen3-30b-a3b-fp8"`, `PROMPT_VERSION = "zh-v1"`, and `SUMMARY_FIELDS = ("objective", "methods", "results", "conclusion")`.

Public functions:

- `source_hash(study: dict) -> str`
- `build_messages(study: dict) -> list[dict[str, str]]`
- `parse_response(text: str) -> dict[str, str]`
- `validate_cache_record(record: dict) -> bool`
- `cache_matches(study: dict, record: dict) -> bool`

- [ ] **Step 1: Write failing tests for hash stability and strict output validation**

Test these cases in `pipeline/tests/test_zh_summary.py`:

- the same title/abstract/model/prompt produces the same SHA-256;
- title or abstract change changes the hash;
- `MODEL_ID` and `PROMPT_VERSION` participate in the hash;
- `build_messages()` contains only PMID, title, and abstract from the study;
- a JSON object with exactly four nonempty Chinese strings succeeds;
- missing field, extra field, empty value, no Chinese character, code fence, `<think>` marker, and a field longer than 600 characters each raise `SummaryValidationError`;
- cache records require `pmid`, `sourceHash`, `model`, `promptVersion`, all four fields, and an ISO `generatedAt` ending in `Z`;
- `cache_matches()` rejects a stale hash, wrong model, or wrong prompt version.

- [ ] **Step 2: Run the tests and confirm the expected import failure**

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_zh_summary -v
```

Expected: failure because `evidence_digest.zh_summary` does not exist.

- [ ] **Step 3: Implement the summary contract and prompt**

Use canonical JSON (`sort_keys=True`, compact separators, UTF-8) for hashing. The system prompt must require one JSON object and no prose:

```python
SYSTEM_PROMPT = """你是医学文献摘要助手。只能依据给定的英文标题和摘要，用简体中文总结；不得补充摘要未提供的数字、样本、机制或结论。摘要未说明的信息写“摘要未说明”。只返回一个 JSON 对象，不要 Markdown，不要解释，不要思考过程。对象必须且只能包含 objective、methods、results、conclusion 四个字符串字段。"""
```

Implement `parse_response()` with `json.loads()`, exact-key comparison, string trimming, 600-character maximum, and these rejection checks before returning the normalized four-field dictionary:

```python
if "```" in text or "<think" in text.casefold() or "</think>" in text.casefold():
    raise SummaryValidationError("model reasoning or markdown is not allowed")
if not re.search(r"[\u3400-\u9fff]", "".join(values)):
    raise SummaryValidationError("summary must contain Chinese text")
```

The hash payload must be exactly:

```python
{
    "title": str(study.get("title") or ""),
    "abstract": str(study.get("abstract") or ""),
    "model": MODEL_ID,
    "promptVersion": PROMPT_VERSION,
}
```

- [ ] **Step 4: Run the focused tests**

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_zh_summary -v
```

Expected: all summary-contract tests pass.

- [ ] **Step 5: Commit the contract**

```bash
git add pipeline/evidence_digest/zh_summary.py pipeline/tests/test_zh_summary.py
git commit -m "feat: define validated Chinese summary contract"
```

---

### Task 3: Add a bounded Cloudflare Workers AI client

**Files:**

- Create: `pipeline/evidence_digest/cloudflare_ai.py`
- Test: `pipeline/tests/test_cloudflare_ai.py`

**Interface:**

- `CloudflareAIError` subclasses `RuntimeError`.
- `CloudflareAIClient(account_id: str, api_token: str, *, opener: Callable = urllib.request.urlopen, sleep_fn: Callable[[float], None] = time.sleep, timeout: float = 30.0)` stores only private fields.
- `CloudflareAIClient.summarize(study: dict) -> dict[str, str]` returns the four validated fields or raises `CloudflareAIError`.

- [ ] **Step 1: Write failing tests using a local fake opener**

The fake opener must capture the `Request` without making a network call. Test:

- URL contains the account ID and exact model path;
- headers contain `Authorization: Bearer <token>` and `Content-Type: application/json`;
- body includes `messages`, `max_tokens: 800`, `temperature: 0.1`, `stream: false`, and `response_format: {"type":"json_object"}`;
- a success envelope whose `result.response` is `{"objective":"研究目的","methods":"研究方法","results":"主要结果","conclusion":"结论"}` returns the validated four fields;
- HTTP 429, HTTP 500, `URLError`, `TimeoutError`, invalid envelope, and invalid model JSON are retryable;
- exactly three total attempts are made, with `sleep_fn` receiving `1.0` then `2.0`;
- HTTP 400 fails immediately and does not retry;
- exception text never includes the API token or full model response.

- [ ] **Step 2: Run and observe the missing-module failure**

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_cloudflare_ai -v
```

Expected: `ModuleNotFoundError` for `evidence_digest.cloudflare_ai`.

- [ ] **Step 3: Implement the stdlib-only client**

Build the request with `urllib.request.Request(url, data=body, headers=headers, method="POST")`; JSON-encode the body as UTF-8. Ask the model for JSON mode with `"response_format": {"type": "json_object"}`. Read at most 1 MiB from the response. Parse the Cloudflare envelope and pass `result.response` to `zh_summary.parse_response()`.

Retry only `HTTPError` codes 429 and 500–599, `URLError`, `TimeoutError`, `json.JSONDecodeError`, `SummaryValidationError`, missing response fields, and a false `success` envelope. After the third failure, raise a sanitized `CloudflareAIError` containing only the failure category and attempt count.

Use this retry skeleton:

```python
for attempt in range(3):
    try:
        return self._request_once(study)
    except _RETRYABLE_ERRORS as exc:
        if isinstance(exc, urllib.error.HTTPError) and not (
            exc.code == 429 or 500 <= exc.code <= 599
        ):
            raise CloudflareAIError(f"non-retryable HTTP {exc.code}") from None
        if attempt == 2:
            raise CloudflareAIError(
                f"Workers AI failed after 3 attempts: {type(exc).__name__}"
            ) from None
        self._sleep_fn(float(2**attempt))
raise AssertionError("unreachable")
```

- [ ] **Step 4: Run the client and summary-contract tests**

```bash
PYTHONPATH=pipeline python -m unittest \
  pipeline.tests.test_cloudflare_ai \
  pipeline.tests.test_zh_summary -v
```

Expected: all tests pass without network access.

- [ ] **Step 5: Commit the client**

```bash
git add pipeline/evidence_digest/cloudflare_ai.py pipeline/tests/test_cloudflare_ai.py
git commit -m "feat: call Cloudflare Workers AI for Chinese summaries"
```

---

### Task 4: Persist valid summaries in a deterministic cache

**Files:**

- Create: `pipeline/evidence_digest/enrichment_store.py`
- Test: `pipeline/tests/test_enrichment_store.py`
- Reuse: `pipeline/evidence_digest/store.py`

**Interface:**

- `cache_dir(paths: Paths = PATHS) -> Path`
- `cache_path(day: date, paths: Paths = PATHS) -> Path`
- `read_latest(paths: Paths = PATHS) -> dict[str, dict]`
- `write_day(day: date, records: list[dict], paths: Paths = PATHS) -> int`

- [ ] **Step 1: Write failing cache tests**

Use the existing scratch-`Paths` test pattern. Verify:

- writes go to `data/enrichment/zh/YYYY-MM-DD.jsonl.gz`;
- a file is valid gzip JSONL and repeated writes with identical logical records are byte-identical;
- records are sorted by numeric PMID before writing;
- a later dated valid record wins for the same PMID;
- a duplicate in the same date file replaces the older source hash rather than adding a second line;
- invalid individual JSON lines and invalid summary records are ignored while adjacent valid records remain readable;
- `read_latest()` returns only records accepted by `zh_summary.validate_cache_record()`.

- [ ] **Step 2: Run and observe the missing-module failure**

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_enrichment_store -v
```

Expected: import failure for the new module.

- [ ] **Step 3: Implement tolerant reads and deterministic writes**

Derive paths from the existing `paths.data_dir`; do not change the `Paths` dataclass. For tolerant reads, open gzip files with `gzip.open(path, "rt", encoding="utf-8")`, catch `json.JSONDecodeError` per line, and reject invalid summary records. For writes:

1. read valid existing records from that day;
2. merge by PMID, with incoming records replacing the same PMID;
3. sort by `(int(pmid), sourceHash)`;
4. delegate the final write to existing deterministic `store._write_jsonl_gz()`;
5. return the number of valid records written.

Do not catch a corrupt gzip container: a truncated whole file is an operational error and should be visible rather than silently erasing the cache.

- [ ] **Step 4: Run cache and existing store tests**

```bash
PYTHONPATH=pipeline python -m unittest \
  pipeline.tests.test_enrichment_store \
  pipeline.tests.test_store -v
```

Expected: all tests pass and repeated-write byte comparison succeeds.

- [ ] **Step 5: Commit the cache layer**

```bash
git add pipeline/evidence_digest/enrichment_store.py pipeline/tests/test_enrichment_store.py
git commit -m "feat: cache Chinese summaries deterministically"
```

---

### Task 5: Orchestrate enrichment and expose the CLI command

**Files:**

- Create: `pipeline/evidence_digest/enrich.py`
- Modify: `pipeline/evidence_digest/cli.py`
- Test: `pipeline/tests/test_enrich.py`

Keep the CLI assertions in `pipeline/tests/test_enrich.py` so this task has one unambiguous test target.

**Interface:**

`EnrichmentResult` is a frozen dataclass with integer fields `eligible`, `cached`, `generated`, `failed`, and `excluded`.

The orchestration signature is `run(*, window_days: int, limit: int = 200, paths: Paths = PATHS, today: date | None = None, client: CloudflareAIClient | None = None, records: list[dict] | None = None) -> EnrichmentResult`.

`client` is required for actual enrichment; `run()` raises `ValueError` when it is `None`. The CLI handles missing secrets before calling `run()`, so missing configuration remains a successful skip rather than an error.

- [ ] **Step 1: Write failing orchestration tests**

With injected records and a fake client, verify:

- eligibility is applied before the fake client is called;
- journals are limited to `config.load_journals(paths).journals` and removed journals are excluded;
- records sort newest first by `(entryDate, score, pmid)`;
- a valid matching cache record suppresses an AI call;
- a stale cache record triggers a call;
- at most `limit` uncached eligible records are called;
- successful summaries are wrapped with `pmid`, `sourceHash`, `model`, `promptVersion`, and a UTC `generatedAt`, then written for `today`;
- one client exception increments `failed`, later records continue, and only successes are written;
- duplicate PMIDs in the archive produce one call;
- a 120-day window excludes older records;
- `limit` outside 1–200 raises `ValueError`.

- [ ] **Step 2: Run and observe the expected failure**

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_enrich -v
```

Expected: import failure for `evidence_digest.enrich`.

- [ ] **Step 3: Implement selection, cache reuse, and soft failure**

In `enrich.run()`:

1. use `today or date.today()` and compute the inclusive window start;
2. use `records` when injected, otherwise `list(store.read_archive(window_days=window_days, today=today, paths=paths))`;
3. deduplicate by PMID, keeping the greatest `(entryDate, score, pmid)` record;
4. build `allowed_journal_tas` from the journal registry;
5. apply `eligibility.assess()`;
6. sort eligible records descending by `(entryDate, score, int(pmid))`;
7. count valid `cache_matches()` records as cached;
8. send only the first `limit` cache misses to the injected client;
9. catch `CloudflareAIError` per record, print `WARNING: Chinese enrichment failed for PMID <pmid>: <sanitized category>`, and continue;
10. persist all successes in one `write_day()` call.

Use UTC for `generatedAt`:

```python
generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
```

- [ ] **Step 4: Add `enrich-zh` to the CLI**

Add parser arguments:

```python
enrich_zh = sub.add_parser("enrich-zh", help="Generate cached Chinese summaries")
enrich_zh.add_argument("--window-days", type=int, default=120)
enrich_zh.add_argument("--limit", type=int, default=200)
enrich_zh.set_defaults(func=cmd_enrich_zh)
```

`cmd_enrich_zh()` must read `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_AI_TOKEN`. If either is absent, print one warning and return exit code 0 without creating a client. Otherwise create `CloudflareAIClient`, call `enrich.run()`, and print exactly one safe summary line:

```text
ZH ENRICHMENT: eligible=<n> cached=<n> generated=<n> failed=<n> excluded=<n>
```

No secret value may appear in stdout, stderr, or an exception.

- [ ] **Step 5: Run focused tests and CLI help**

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_enrich -v
PYTHONPATH=pipeline python -m evidence_digest.cli enrich-zh --help
```

Expected: tests pass; help includes `--window-days` and `--limit`.

- [ ] **Step 6: Commit orchestration and CLI**

```bash
git add pipeline/evidence_digest/enrich.py pipeline/evidence_digest/cli.py pipeline/tests/test_enrich.py
git commit -m "feat: add Chinese enrichment command"
```

---

### Task 6: Generate the Chinese Atom feed without changing English feeds

**Files:**

- Modify: `pipeline/evidence_digest/feeds.py`
- Modify: `pipeline/evidence_digest/build.py`
- Modify: `pipeline/tests/test_feeds.py`
- Modify: `pipeline/tests/test_build.py`

**Interface:**

The serializer signature is `write_chinese_feed(path: Path, *, studies: list[dict], summaries: dict[str, dict], site_url: str, generated_at: str) -> None`.

- [ ] **Step 1: Add failing serializer tests**

Extend `pipeline/tests/test_feeds.py` with a study containing XML-sensitive text (`&`, `<`) and a valid summary. Assert:

- ElementTree parses the result;
- feed title is `Evidence Digest — 中文精选`;
- entry title is `【Nat Immunol】English & escaped title`;
- entry ID is `urn:evidence-digest:zh:<pmid>`;
- entry `updated` equals cache `generatedAt` and `published` equals study `entryDate` in Atom time format;
- plain `summary` contains `研究目的：`, `研究方法：`, `主要结果：`, `结论：`, and the PubMed URL;
- HTML `content` contains the same four labels plus a clickable escaped PubMed link;
- category includes each existing topic;
- two studies with the same PMID do not create duplicate entries.

Before changing production code, serialize a representative English feed to a fixture string inside the test and retain a byte-equality assertion after the new function is added.

- [ ] **Step 2: Run and confirm the missing-function failure**

```bash
PYTHONPATH=pipeline python -m unittest pipeline.tests.test_feeds -v
```

Expected: import or attribute failure for `write_chinese_feed`; existing English tests remain green.

- [ ] **Step 3: Implement the Chinese serializer beside the English serializer**

Reuse the module's Atom namespace/date helpers. Build text with four fixed labels. Build HTML using `html.escape(value, quote=True)` for every title, field, and URL. Create the content element with `ET.SubElement(entry, f"{{{ATOM_NS}}}content")`, call `content.set("type", "html")`, and assign the escaped HTML string as its text.

The entry title must use:

```python
journal_ta = str((study.get("journal") or {}).get("ta") or "Unknown journal")
entry_title = f"【{journal_ta}】{study['title']}"
```

Sort and limit outside this function; inside it, preserve the passed order and skip any PMID without a summary.

- [ ] **Step 4: Add failing build integration tests**

Extend the scratch build fixture so it can create `data/enrichment/zh/<date>.jsonl.gz`. Add tests proving:

- `build.build()` creates `data/feeds/selected-journals-zh.xml`;
- excluded, removed-journal, no-abstract, stale-summary, and missing-summary records are absent;
- an eligible record with a matching valid cache is present;
- results use the existing build sort order and cap at 200;
- `build.build(records=records)` remains deterministic;
- all pre-existing English feed bytes are identical with and without enrichment cache.

- [ ] **Step 5: Join archive records to summaries in `build.py`**

After existing English feeds are written:

1. load latest summaries with `enrichment_store.read_latest(paths)`;
2. derive allowed journal TAs from current journal config;
3. select records for which `eligibility.assess(study, allowed_journal_tas).eligible` and `zh_summary.cache_matches(study, summaries[study["pmid"]])` are both true;
4. use the existing `_sort_key` to sort descending;
5. deduplicate by PMID and slice to 200;
6. call `write_chinese_feed(paths.feeds_dir / "selected-journals-zh.xml", studies=zh_records, summaries=summaries, site_url=site_url, generated_at=generated_at)`;
7. add one to the returned `feed_count` without changing any existing counts or English paths.

If test isolation requires injection, add only this backward-compatible keyword to `build.build()`:

```python
zh_summaries: dict[str, dict] | None = None
```

When it is `None`, load the cache; when provided, use the injected dictionary.

- [ ] **Step 6: Run feed/build tests and validate XML**

```bash
PYTHONPATH=pipeline python -m unittest \
  pipeline.tests.test_feeds \
  pipeline.tests.test_build -v
PYTHONPATH=pipeline python - <<'PY'
from pathlib import Path
import xml.etree.ElementTree as ET
path = Path("data/feeds/selected-journals-zh.xml")
if path.exists():
    ET.parse(path)
print("Atom XML validation passed")
PY
```

Expected: unit tests pass; XML validation prints `Atom XML validation passed`.

- [ ] **Step 7: Commit the feed/build integration**

```bash
git add pipeline/evidence_digest/feeds.py pipeline/evidence_digest/build.py \
  pipeline/tests/test_feeds.py pipeline/tests/test_build.py
git commit -m "feat: publish filtered Chinese Atom feed"
```

---

### Task 7: Schedule weekly enrichment and preserve manual refresh

**Files:**

- Modify: `.github/workflows/harvest.yml`
- Modify: `.github/workflows/pages.yml`

The user approved the configuration-file exception to TDD on 2026-08-05. Do not add a permanent test that greps YAML source lines. Validate the parsed workflow structure after editing, then use a real GitHub Actions run as the behavioral acceptance test.

- [ ] **Step 1: Update `harvest.yml`**

Apply these exact semantics:

```yaml
on:
  schedule:
    - cron: '0 15 * * 0'
  workflow_dispatch:
    inputs:
      days:
        description: 'PubMed lookback window in days'
        required: false
        default: '8'
```

Change the harvest fallback to 8 days. Immediately after harvest and before the commit step, add:

```yaml
      - name: Enrich eligible studies in Chinese
        if: ${{ !cancelled() }}
        env:
          PYTHONPATH: pipeline
          CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
          CLOUDFLARE_AI_TOKEN: ${{ secrets.CLOUDFLARE_AI_TOKEN }}
        run: python -m evidence_digest.cli enrich-zh --limit 200
```

Extend the existing `mkdir -p` command with `data/enrichment/zh`, and extend the staging command with `data/enrichment`. This prevents an empty or skipped first enrichment from making `git add` fail on a missing path. Update the nearby comment that currently says the job changes only archive/state. Do not change Pages permissions or existing deploy steps.

- [ ] **Step 2: Update `pages.yml` and validate parsed workflow semantics**

Add `data/enrichment/**` to push path filters. Then parse both files with Ruby's standard YAML parser and validate the resulting workflow objects:

```bash
ruby -ryaml <<'RUBY'
harvest = YAML.load_file('.github/workflows/harvest.yml')
pages = YAML.load_file('.github/workflows/pages.yml')

# Ruby/Psych uses YAML 1.1 and parses the unquoted GitHub key `on` as true.
events = harvest.fetch(true)
raise 'weekly cron missing' unless events.fetch('schedule') == [{'cron' => '0 15 * * 0'}]
dispatch = events.fetch('workflow_dispatch')
raise 'manual default must be 8' unless dispatch.fetch('inputs').fetch('days').fetch('default') == '8'

steps = harvest.fetch('jobs').fetch('harvest').fetch('steps')
enrich = steps.find { |step| step['name'] == 'Enrich eligible studies in Chinese' }
raise 'enrichment step missing' unless enrich
raise 'wrong enrichment command' unless enrich.fetch('run') == 'python -m evidence_digest.cli enrich-zh --limit 200'
raise 'account secret missing' unless enrich.fetch('env').key?('CLOUDFLARE_ACCOUNT_ID')
raise 'AI token secret missing' unless enrich.fetch('env').key?('CLOUDFLARE_AI_TOKEN')

page_paths = pages.fetch(true).fetch('push').fetch('paths')
raise 'enrichment path trigger missing' unless page_paths.include?('data/enrichment/**')
puts 'workflow semantic validation passed'
RUBY
```

Expected: `workflow semantic validation passed`.

- [ ] **Step 3: Commit workflow changes**

```bash
git add .github/workflows/harvest.yml .github/workflows/pages.yml
git commit -m "ci: run Chinese digest weekly and on demand"
```

---

### Task 8: Document setup and operational recovery

**Files:**

- Modify: `README.md`

- [ ] **Step 1: Add the user-facing feed and setup instructions**

Document:

- feed URL `https://Yy-glitch0238.github.io/evidence-digest-1/feeds/selected-journals-zh.xml`;
- card title and four-section Chinese body format;
- exclusions and the abstract requirement;
- Cloudflare model ID;
- repository secrets `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_AI_TOKEN`;
- exact GitHub path: `Settings → Secrets and variables → Actions → New repository secret`;
- automatic schedule: Sunday 23:00 Beijing / Sunday 15:00 UTC;
- first scheduled run: 2026-08-09 23:00 Beijing, provided the workflow reaches `main` before that time;
- manual path: `Actions → Harvest → Run workflow`, with 8 days as the normal value;
- first run may create fewer than 51 entries because non-research items and no-abstract items are removed;
- an AI failure leaves that article out temporarily and retries it on a later run;
- missing secrets skip new AI work but do not break the English feeds or previously cached Chinese entries;
- rollback is simply resubscribing to the existing English feed.

- [ ] **Step 2: Check documentation for secret leakage and exact naming**

```bash
rg -n "CLOUDFLARE_|selected-journals-zh|0 15|23:00|Run workflow" README.md
```

Expected: only secret *names* appear, never actual values; the feed URL and both times are present.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md
git commit -m "docs: explain Chinese feed setup and refresh"
```

---

### Task 9: Full verification and one controlled live smoke test

**Files:**

- Verify only; do not edit unless a test exposes a defect.

- [ ] **Step 1: Run the full Python suite and compilation check**

```bash
PYTHONPATH=pipeline python -m unittest discover -s pipeline/tests -v
python -m compileall pipeline/evidence_digest pipeline/tests
```

Expected: every Python test passes and compilation exits 0.

- [ ] **Step 2: Run the existing web, worker, and contract checks exactly as CI does**

Run:

```bash
if [ -f web/package-lock.json ]; then
  (cd web && npm ci)
else
  (cd web && npm install)
fi
(cd web && npx tsc --noEmit)
(cd web && npm run build)

if [ -f worker/package-lock.json ]; then
  (cd worker && npm ci)
else
  (cd worker && npm install)
fi
(cd worker && npx tsc --noEmit)

node scripts/check-contract.mjs
```

Expected: dependency installation, web type-check/build, worker type-check, and schema/type parity all exit 0.

- [ ] **Step 3: Prove existing English output did not regress**

Build once with no enrichment cache and once with a valid cache in a scratch data directory. Compare every pre-existing English `.xml` file byte-for-byte; exclude only `selected-journals-zh.xml` from the comparison.

Expected: no English feed diff.

- [ ] **Step 4: Validate the generated Chinese Atom file**

```bash
PYTHONPATH=pipeline python -m evidence_digest.cli build
python - <<'PY'
from pathlib import Path
import xml.etree.ElementTree as ET

path = Path("data/feeds/selected-journals-zh.xml")
root = ET.parse(path).getroot()
ns = {"atom": "http://www.w3.org/2005/Atom"}
entries = root.findall("atom:entry", ns)
assert len(entries) <= 200
for entry in entries:
    assert entry.findtext("atom:title", default="", namespaces=ns).startswith("【")
    summary = entry.findtext("atom:summary", default="", namespaces=ns)
    for label in ("研究目的：", "研究方法：", "主要结果：", "结论："):
        assert label in summary
print(f"validated {len(entries)} Chinese Atom entries")
PY
```

Expected: the file parses, contains at most 200 entries, and every entry has all four labels.

- [ ] **Step 5: Push to a feature branch and let GitHub CI verify the same commit**

```bash
git status --short
git log --oneline --decorate -10
git branch --show-current
git push -u origin feat/chinese-filtered-feed
```

Expected: clean working tree, current branch `feat/chinese-filtered-feed`, and all GitHub Actions checks turn green. If any CI job differs from local behavior, inspect its full log and fix the root cause before deployment.

- [ ] **Step 6: Configure secrets and run one manual 8-day smoke test**

In GitHub repository settings, add `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_AI_TOKEN`. Then run `Actions → Harvest → Run workflow` with `days=8`.

Expected workflow ordering and results:

1. harvest succeeds;
2. `ZH ENRICHMENT` reports counts without exposing secrets;
3. `data/enrichment/zh/<date>.jsonl.gz` is committed only when summaries succeeded;
4. Pages deployment succeeds even if individual AI calls failed;
5. the public Atom URL returns parseable XML.

- [ ] **Step 7: Perform Inoreader acceptance**

Subscribe to `https://Yy-glitch0238.github.io/evidence-digest-1/feeds/selected-journals-zh.xml`. Confirm at least one card title uses `【MEDLINE 缩写】英文标题`; open the card and confirm all four Chinese sections and the PubMed link. Confirm known news/correction samples are absent.

- [ ] **Step 8: Commit any verification-only fix, then open the PR**

If no fixes were needed, do not create an empty commit. If a fix was required, rerun all checks and commit it with a narrow message. Then run:

```bash
gh pr create --draft \
  --base main \
  --head feat/chinese-filtered-feed \
  --title "Add filtered Chinese journal feed" \
  --body "Adds deterministic research-only filtering, cached Cloudflare Workers AI Chinese summaries, a new Atom feed, weekly Sunday 23:00 Beijing scheduling, and manual 8-day refresh. Existing English outputs remain unchanged."
```

Expected: a draft PR opens and lists the new feed, deterministic exclusions, cache behavior, Cloudflare requirements, and Sunday schedule.

---

## Definition of Done

- All Python, web, worker, and contract tests pass locally and in GitHub Actions.
- The scheduled cron is `0 15 * * 0`; manual dispatch remains available with an 8-day default.
- The Chinese feed contains only current configured journals, nonempty abstracts, allowed publication types, allowed titles, and valid matching cached summaries.
- The Chinese feed contains at most 200 entries, uses stable IDs, shows MEDLINE abbreviations, four Chinese fields, and PubMed links.
- Cache hits make zero AI calls; stale or missing hashes are regenerated; a single failed article does not fail the run.
- Existing English web/API/feed behavior is unchanged.
- The feed is successfully subscribed to and visually checked in Inoreader.
