"""Load and validate the three editorial config files.

Why this module exists: journals.json, taxonomy/*.json, and scoring.json are the
only things a non-programmer editor ever needs to touch to change what Evidence
Digest covers or how it ranks. Every other module treats them as opaque, already
-validated data. Putting all structural validation in one place means a broken
config fails loudly and specifically (which file, which field) the moment
`cli.py validate` runs in CI, instead of surfacing as a confusing KeyError three
modules downstream during a live harvest.

We hand-roll validation instead of depending on the `jsonschema` package because
the pipeline must run on a bare GitHub Actions runner with no `pip install` step
(see contract/config.schema.json for the schema this mirrors).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9-]+$")

# The exact evidence-level vocabulary fixed by contract/study.schema.json's
# `evidence.level` enum. scoring.json must supply rank/label/points for exactly
# these, because score.py's detect_evidence() only ever returns one of them.
KNOWN_EVIDENCE_LEVELS = frozenset(
    {
        "guideline",
        "meta-analysis",
        "rct",
        "trial",
        "observational",
        "review",
        "basic",
        "case-report",
        "other",
    }
)

REQUIRED_LIMITS_KEYS = (
    "servedWindowDays",
    "perTopicFile",
    "highlights",
    "highlightsPerSpecialty",
    "searchIndexMinScore",
    "searchIndexWindowDays",
    "feedItems",
    "emailStudiesPerTopic",
    "emailMaxStudies",
)


class ConfigError(Exception):
    """Raised for any structural or cross-file problem in the editorial config.

    Messages are written to be pasted directly into a PR comment: they name the
    offending file and, where relevant, the offending sibling file too.
    """


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Paths:
    """Every filesystem location the pipeline reads or writes, resolved once
    relative to this package file so the CLI behaves the same from any cwd."""

    repo_root: Path
    pipeline_dir: Path
    config_dir: Path
    taxonomy_dir: Path
    journals_path: Path
    scoring_path: Path
    contract_dir: Path
    data_dir: Path
    archive_dir: Path
    state_dir: Path
    seen_dir: Path
    runs_path: Path
    api_dir: Path
    feeds_dir: Path


def _default_paths() -> Paths:
    package_dir = Path(__file__).resolve().parent
    pipeline_dir = package_dir.parent
    repo_root = pipeline_dir.parent
    config_dir = pipeline_dir / "config"
    data_dir = repo_root / "data"
    state_dir = data_dir / "state"
    return Paths(
        repo_root=repo_root,
        pipeline_dir=pipeline_dir,
        config_dir=config_dir,
        taxonomy_dir=config_dir / "taxonomy",
        journals_path=config_dir / "journals.json",
        scoring_path=config_dir / "scoring.json",
        contract_dir=repo_root / "contract",
        data_dir=data_dir,
        archive_dir=data_dir / "archive",
        state_dir=state_dir,
        seen_dir=state_dir / "seen",
        runs_path=state_dir / "runs.json",
        api_dir=data_dir / "api",
        feeds_dir=data_dir / "feeds",
    )


PATHS = _default_paths()


# --------------------------------------------------------------------------- #
# taxonomy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Topic:
    slug: str
    name: str
    blurb: str
    catch_all: bool
    mesh: tuple[str, ...]
    phrases: tuple[str, ...]
    acronyms: tuple[str, ...]
    veto: tuple[str, ...]
    specialty_slug: str
    specialty_order: int
    order_in_specialty: int


@dataclass(frozen=True)
class Specialty:
    slug: str
    order: int
    name: str
    icon: str
    blurb: str
    topics: tuple[Topic, ...]

    @property
    def catch_all_topic(self) -> Topic:
        for topic in self.topics:
            if topic.catch_all:
                return topic
        # load_taxonomy() guarantees exactly one catchAll per specialty, so this
        # is unreachable for any Taxonomy that passed validation.
        raise ConfigError(f"specialty '{self.slug}' has no catchAll topic")


@dataclass(frozen=True)
class Taxonomy:
    specialties: tuple[Specialty, ...]

    @property
    def specialty_by_slug(self) -> dict[str, Specialty]:
        return {s.slug: s for s in self.specialties}

    @property
    def topic_by_slug(self) -> dict[str, Topic]:
        out: dict[str, Topic] = {}
        for s in self.specialties:
            for t in s.topics:
                out[t.slug] = t
        return out

    @property
    def all_topics(self) -> list[Topic]:
        out: list[Topic] = []
        for s in self.specialties:
            out.extend(s.topics)
        return out


def _require_keys(obj: dict, required: tuple[str, ...], where: str) -> None:
    missing = [k for k in required if k not in obj]
    if missing:
        raise ConfigError(f"{where}: missing required key(s) {missing}")


def _load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from exc


def _validate_rules(rules: dict, where: str) -> tuple[list[str], list[str], list[str], list[str]]:
    if not isinstance(rules, dict):
        raise ConfigError(f"{where}: 'rules' must be an object")
    allowed = {"mesh", "phrases", "acronyms", "not"}
    extra = set(rules) - allowed
    if extra:
        raise ConfigError(f"{where}: rules has unexpected key(s) {sorted(extra)}")
    out = []
    for key in ("mesh", "phrases", "acronyms", "not"):
        values = rules.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ConfigError(f"{where}: rules.{key} must be a list of strings")
        out.append(values)
    return tuple(out)  # type: ignore[return-value]


def load_taxonomy(paths: Paths = PATHS) -> Taxonomy:
    """Glob pipeline/config/taxonomy/*.json, validate each file structurally,
    then cross-validate the set: globally unique topic slugs and exactly one
    catchAll per specialty. Specialties are returned sorted by `order`."""

    if not paths.taxonomy_dir.is_dir():
        raise ConfigError(f"taxonomy directory not found: {paths.taxonomy_dir}")

    files = sorted(paths.taxonomy_dir.glob("*.json"))
    if not files:
        raise ConfigError(f"no taxonomy files found in {paths.taxonomy_dir}")

    specialties: list[Specialty] = []
    slug_origin: dict[str, Path] = {}  # topic slug -> file it came from

    for file_path in files:
        data = _load_json(file_path)
        where = str(file_path)
        _require_keys(data, ("slug", "order", "name", "icon", "blurb", "topics"), where)

        slug = data["slug"]
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise ConfigError(f"{where}: specialty slug '{slug}' must match {SLUG_RE.pattern}")
        if slug != file_path.stem:
            raise ConfigError(
                f"{where}: specialty slug '{slug}' must equal the filename stem "
                f"'{file_path.stem}'"
            )

        order = data["order"]
        if not isinstance(order, int):
            raise ConfigError(f"{where}: 'order' must be an integer")

        topics_raw = data["topics"]
        if not isinstance(topics_raw, list) or not topics_raw:
            raise ConfigError(f"{where}: 'topics' must be a non-empty list")

        topics: list[Topic] = []
        catch_all_count = 0
        for idx, topic_raw in enumerate(topics_raw):
            topic_where = f"{where} topics[{idx}]"
            _require_keys(topic_raw, ("slug", "name", "blurb", "rules"), topic_where)
            t_slug = topic_raw["slug"]
            if not isinstance(t_slug, str) or not SLUG_RE.match(t_slug):
                raise ConfigError(
                    f"{topic_where}: topic slug '{t_slug}' must match {SLUG_RE.pattern}"
                )
            if t_slug in slug_origin:
                raise ConfigError(
                    f"topic slug '{t_slug}' is defined twice: "
                    f"{slug_origin[t_slug]} and {file_path}"
                )
            slug_origin[t_slug] = file_path

            catch_all = bool(topic_raw.get("catchAll", False))
            if catch_all:
                catch_all_count += 1
            mesh, phrases, acronyms, veto = _validate_rules(topic_raw["rules"], topic_where)

            topics.append(
                Topic(
                    slug=t_slug,
                    name=str(topic_raw["name"]),
                    blurb=str(topic_raw["blurb"]),
                    catch_all=catch_all,
                    mesh=tuple(mesh),
                    phrases=tuple(phrases),
                    acronyms=tuple(acronyms),
                    veto=tuple(veto),
                    specialty_slug=slug,
                    specialty_order=order,
                    order_in_specialty=idx,
                )
            )

        if catch_all_count != 1:
            raise ConfigError(
                f"{where}: specialty '{slug}' must have exactly one catchAll topic, "
                f"found {catch_all_count}"
            )

        specialties.append(
            Specialty(
                slug=slug,
                order=order,
                name=str(data["name"]),
                icon=str(data["icon"]),
                blurb=str(data["blurb"]),
                topics=tuple(topics),
            )
        )

    specialties.sort(key=lambda s: s.order)
    return Taxonomy(specialties=tuple(specialties))


# --------------------------------------------------------------------------- #
# journals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Journal:
    name: str
    ta: str
    specialty: str
    tier: int
    scope: str
    note: str | None = None


@dataclass(frozen=True)
class JournalsConfig:
    version: int
    topic_filter: str
    journals: tuple[Journal, ...]
    virus_filter: str | None = None


def load_journals(paths: Paths = PATHS) -> JournalsConfig:
    """Load and structurally validate pipeline/config/journals.json. Does NOT
    cross-check journal specialties against the taxonomy — use load_all() for
    that, since it needs both files."""

    data = _load_json(paths.journals_path)
    where = str(paths.journals_path)
    _require_keys(data, ("version", "journals", "topicFilter"), where)

    virus_filter = data.get("virusFilter")
    if virus_filter is not None and (not isinstance(virus_filter, str) or not virus_filter.strip()):
        raise ConfigError(f"{where}: 'virusFilter' must be a non-empty string")

    journals_raw = data["journals"]
    if not isinstance(journals_raw, list) or not journals_raw:
        raise ConfigError(f"{where}: 'journals' must be a non-empty list")

    journals: list[Journal] = []
    seen_ta: dict[str, int] = {}
    for idx, j in enumerate(journals_raw):
        j_where = f"{where} journals[{idx}]"
        _require_keys(j, ("name", "ta", "specialty", "tier", "scope"), j_where)

        specialty = j["specialty"]
        if not isinstance(specialty, str) or not SLUG_RE.match(specialty):
            raise ConfigError(
                f"{j_where}: specialty '{specialty}' must match {SLUG_RE.pattern}"
            )
        tier = j["tier"]
        if tier not in (1, 2, 3):
            raise ConfigError(f"{j_where}: tier must be 1, 2, or 3, got {tier!r}")
        scope = j["scope"]
        if scope not in ("all", "topic", "virus"):
            raise ConfigError(f"{j_where}: scope must be 'all', 'topic', or 'virus', got {scope!r}")
        if scope == "virus" and virus_filter is None:
            raise ConfigError(f"{j_where}: scope 'virus' requires a top-level 'virusFilter'")
        ta = j["ta"]
        if not isinstance(ta, str) or not ta.strip():
            raise ConfigError(f"{j_where}: 'ta' must be a non-empty string")
        if ta in seen_ta:
            raise ConfigError(
                f"{j_where}: duplicate journal ta '{ta}' (also at index {seen_ta[ta]})"
            )
        seen_ta[ta] = idx

        journals.append(
            Journal(
                name=str(j["name"]),
                ta=ta,
                specialty=specialty,
                tier=tier,
                scope=scope,
                note=j.get("note"),
            )
        )

    return JournalsConfig(
        version=int(data["version"]),
        topic_filter=str(data["topicFilter"]),
        journals=tuple(journals),
        virus_filter=virus_filter,
    )


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceLevelInfo:
    rank: int
    label: str
    points: int


@dataclass(frozen=True)
class ScoringConfig:
    version: int
    assign_threshold: int
    weights: dict[str, int]  # mesh, title, keywords, abstract
    journal_tier: dict[int, int]  # 1, 2, 3 -> points
    evidence_levels: dict[str, EvidenceLevelInfo]
    bonuses: dict[str, int]
    penalties: dict[str, int]
    recency_max_points: int
    recency_half_life_days: int
    limits: dict[str, int]


def _strip_meta(d: dict) -> dict:
    """scoring.json documents itself with sibling `_comment` strings inside
    several nested objects (journalTier, bonuses, penalties, limits,
    classifier, recency). Those are documentation, not data - drop any
    underscore-prefixed key before structural validation or type coercion
    ever sees it."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def load_scoring(paths: Paths = PATHS) -> ScoringConfig:
    data = _load_json(paths.scoring_path)
    where = str(paths.scoring_path)
    _require_keys(
        data,
        (
            "version",
            "classifier",
            "journalTier",
            "evidenceLevels",
            "bonuses",
            "penalties",
            "recency",
            "limits",
        ),
        where,
    )

    classifier = _strip_meta(data["classifier"])
    _require_keys(classifier, ("assignThreshold", "weights"), f"{where} classifier")
    weights = _strip_meta(classifier["weights"])
    _require_keys(weights, ("mesh", "title", "keywords", "abstract"), f"{where} classifier.weights")
    extra_weights = set(weights) - {"mesh", "title", "keywords", "abstract"}
    if extra_weights:
        raise ConfigError(f"{where}: classifier.weights has unexpected key(s) {sorted(extra_weights)}")

    journal_tier_raw = _strip_meta(data["journalTier"])
    _require_keys(journal_tier_raw, ("1", "2", "3"), f"{where} journalTier")
    journal_tier = {int(k): int(v) for k, v in journal_tier_raw.items()}

    evidence_levels_raw = data["evidenceLevels"]
    got_levels = set(evidence_levels_raw)
    if got_levels != KNOWN_EVIDENCE_LEVELS:
        missing = KNOWN_EVIDENCE_LEVELS - got_levels
        extra = got_levels - KNOWN_EVIDENCE_LEVELS
        raise ConfigError(
            f"{where}: evidenceLevels must define exactly {sorted(KNOWN_EVIDENCE_LEVELS)}; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )
    evidence_levels: dict[str, EvidenceLevelInfo] = {}
    ranks_seen: dict[int, str] = {}
    for level, info in evidence_levels_raw.items():
        lvl_where = f"{where} evidenceLevels.{level}"
        _require_keys(info, ("rank", "label", "points"), lvl_where)
        rank = int(info["rank"])
        if rank in ranks_seen:
            raise ConfigError(
                f"{lvl_where}: rank {rank} duplicates the rank of '{ranks_seen[rank]}'"
            )
        ranks_seen[rank] = level
        evidence_levels[level] = EvidenceLevelInfo(
            rank=rank, label=str(info["label"]), points=int(info["points"])
        )
    expected_ranks = set(range(1, len(KNOWN_EVIDENCE_LEVELS) + 1))
    if set(ranks_seen) != expected_ranks:
        raise ConfigError(
            f"{where}: evidenceLevels ranks must cover exactly {sorted(expected_ranks)}, "
            f"got {sorted(ranks_seen)}"
        )

    recency = _strip_meta(data["recency"])
    _require_keys(recency, ("maxPoints", "halfLifeDays"), f"{where} recency")

    limits = _strip_meta(data["limits"])
    _require_keys(limits, REQUIRED_LIMITS_KEYS, f"{where} limits")
    extra_limits = set(limits) - set(REQUIRED_LIMITS_KEYS)
    if extra_limits:
        raise ConfigError(f"{where}: limits has unexpected key(s) {sorted(extra_limits)}")

    bonuses = _strip_meta(data["bonuses"])
    penalties = _strip_meta(data["penalties"])
    if not isinstance(bonuses, dict) or not all(isinstance(v, int) for v in bonuses.values()):
        raise ConfigError(f"{where}: bonuses must be an object of integers")
    if not isinstance(penalties, dict) or not all(isinstance(v, int) for v in penalties.values()):
        raise ConfigError(f"{where}: penalties must be an object of integers")

    return ScoringConfig(
        version=int(data["version"]),
        assign_threshold=int(classifier["assignThreshold"]),
        weights={k: int(v) for k, v in weights.items()},
        journal_tier=journal_tier,
        evidence_levels=evidence_levels,
        bonuses={k: int(v) for k, v in bonuses.items()},
        penalties={k: int(v) for k, v in penalties.items()},
        recency_max_points=int(recency["maxPoints"]),
        recency_half_life_days=int(recency["halfLifeDays"]),
        limits={k: int(v) for k, v in limits.items()},
    )


# --------------------------------------------------------------------------- #
# aggregate load + cross-validation
# --------------------------------------------------------------------------- #


def load_all(paths: Paths = PATHS) -> tuple[JournalsConfig, Taxonomy, ScoringConfig]:
    """Load all three configs and cross-validate them against each other:
    every journal's specialty must resolve to a loaded taxonomy specialty. This
    is the entry point `cli.py validate` (and every command that touches
    journals or classification) should use."""

    journals = load_journals(paths)
    taxonomy = load_taxonomy(paths)
    scoring = load_scoring(paths)

    known_specialties = set(taxonomy.specialty_by_slug)
    bad = sorted(
        {j.specialty for j in journals.journals if j.specialty not in known_specialties}
    )
    if bad:
        raise ConfigError(
            f"{paths.journals_path}: journal specialty slug(s) {bad} do not match any "
            f"specialty in {paths.taxonomy_dir} (known: {sorted(known_specialties)})"
        )

    return journals, taxonomy, scoring
