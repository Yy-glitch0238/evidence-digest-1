/**
 * Evidence Digest — shared TypeScript contract.
 *
 * This is the hand-maintained mirror of `contract/study.schema.json` and
 * `contract/api.schema.json`. Both the web app and the Cloudflare Worker import
 * from here so that a change to the data shape breaks the type-check in one
 * place instead of silently breaking a page or an email.
 *
 * `scripts/check-contract.mjs` asserts field parity between this file and the
 * JSON Schemas on every CI run, so the two cannot drift.
 */

/** Study-design bucket, inferred from PubMed publication types, then MeSH, then title/abstract cues. */
export type EvidenceLevel =
  | 'guideline'
  | 'meta-analysis'
  | 'rct'
  | 'trial'
  | 'observational'
  | 'review'
  | 'basic'
  | 'case-report'
  | 'other';

export interface EvidenceGrade {
  level: EvidenceLevel;
  /** Display label, e.g. "Randomized trial". */
  label: string;
  /** 1 = strongest design bucket, 9 = weakest. Used for sorting and badges. */
  rank: number;
}

export interface JournalRef {
  name: string;
  /** MEDLINE title abbreviation used in the PubMed [ta] query. */
  ta: string;
  /** 1 = practice-defining/flagship, 2 = leading subspecialty, 3 = solid specialty. */
  tier: 1 | 2 | 3;
}

/**
 * One study. Every key is always present; absent information is `null` or `[]`.
 * Mirrors contract/study.schema.json exactly.
 */
export interface Study {
  pmid: string;
  doi: string | null;
  title: string;
  abstract: string;
  /** Structured abstract sections keyed by uppercased NLM label. */
  sections: Record<string, string>;
  /** Deterministically extracted 1–2 sentence "why this matters". No model involved. */
  takeaway: string;
  authors: string[];
  /** Up to three authors then ", et al". */
  authorLine: string;
  journal: JournalRef;
  specialties: string[];
  topics: string[];
  pubTypes: string[];
  evidence: EvidenceGrade;
  /** Deterministic importance score, 0–100. */
  score: number;
  /** Best available publication date: YYYY-MM-DD, YYYY-MM, or YYYY. */
  pubdate: string;
  /** PubMed Entrez date (EDAT) — the day the record was indexed. Sorting key. */
  entryDate: string;
  url: string;
  doiUrl: string | null;
  pmcid: string | null;
  openAccess: boolean;
  mesh: string[];
  keywords: string[];
  trialIds: string[];
  hasAbstract: boolean;
}

/**
 * The trimmed study shape carried in `api/topics/*.json` and `api/highlights.json`.
 *
 * Full abstracts, MeSH, and keyword lists are deliberately omitted: with ~100
 * topics served daily, shipping abstracts in every topic file would multiply the
 * payload roughly tenfold for data that only the detail view and the search box
 * inside a loaded topic ever use.
 */
export type StudyCard = Omit<Study, 'abstract' | 'sections' | 'mesh' | 'keywords'>;

export interface Manifest {
  /** Bumped on a breaking change to the study shape; clients refuse unknown majors. */
  dataVersion: number;
  generatedAt: string;
  windowDays: number;
  /** Every served entryDate, newest first. */
  days: string[];
  latestDay: string;
  totalStudies: number;
  journalCount: number;
  /** topicSlug → count in the served window. Drives the picker's counts. */
  topicCounts: Record<string, number>;
  specialtyCounts: Record<string, number>;
}

export interface TopicFile {
  topic: string;
  specialty: string;
  generatedAt: string;
  /** Total in the served window, before the per-file cap. */
  total: number;
  /** How many are actually in this file. */
  returned: number;
  studies: StudyCard[];
}

export interface DayFile {
  date: string;
  generatedAt: string;
  total: number;
  /** Full studies, including abstracts — this is the detail source. */
  studies: Study[];
}

export interface HighlightsFile {
  generatedAt: string;
  since: string;
  studies: StudyCard[];
}

export interface SearchEntry {
  /** pmid */
  p: string;
  /** title */
  t: string;
  /** journal name */
  j: string;
  /** entryDate */
  d: string;
  /** score */
  s: number;
  /** topic slugs */
  tp: string[];
}

export interface SearchIndexFile {
  generatedAt: string;
  minScore: number;
  windowDays: number;
  entries: SearchEntry[];
}

export interface PublicTopic {
  slug: string;
  name: string;
  blurb: string;
}

export interface PublicSpecialty {
  slug: string;
  name: string;
  icon: string;
  blurb: string;
  topics: PublicTopic[];
}

export interface PublicTaxonomy {
  version: number;
  specialties: PublicSpecialty[];
}

export interface PublicJournal {
  name: string;
  ta: string;
  specialty: string;
  tier: number;
  scope: 'all' | 'topic' | 'virus';
  pubmedUrl: string;
}

export interface PublicJournalsFile {
  generatedAt: string;
  count: number;
  journals: PublicJournal[];
}

/** How often a subscriber receives their digest. */
export type Frequency = 'daily' | 'weekly' | 'monthly';

/** Subscriber-facing preference payload, shared by the signup form and the Worker API. */
export interface SubscriptionPrefs {
  topics: string[];
  frequency: Frequency;
  /** Only include studies at or above this score. 0 = send everything matched. */
  minScore?: number;
  /** IANA timezone, used to decide when "today" starts for that reader. */
  timezone?: string;
}
