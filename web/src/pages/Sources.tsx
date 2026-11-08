import { useMemo, useState } from 'react';
import { useJournals } from '../hooks/useApi';
import { useDocumentTitle } from '../hooks/useDocumentTitle';
import { ErrorState } from '../components/ErrorState';
import { Skeleton } from '../components/Skeleton';
import { Select } from '../components/Select';

type SortColumn = 'name' | 'ta' | 'specialty' | 'tier' | 'scope';
type SortDirection = 'asc' | 'desc';

function humanize(slug: string): string {
  return slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

const TIER_LABEL: Record<number, string> = {
  1: 'Tier 1 — flagship',
  2: 'Tier 2 — leading subspecialty',
  3: 'Tier 3 — specialty',
};

export default function Sources() {
  useDocumentTitle(
    'Sources',
    'Every journal Evidence Digest watches, with its tier and coverage — the basis for trusting the digest.',
  );

  const [journalsState, retry] = useJournals();
  const [query, setQuery] = useState('');
  const [specialty, setSpecialty] = useState('all');
  const [tier, setTier] = useState('all');
  const [scope, setScope] = useState('');
  const [sort, setSort] = useState<{ column: SortColumn; direction: SortDirection }>({
    column: 'tier',
    direction: 'asc',
  });

  const specialties = useMemo(() => {
    if (journalsState.status !== 'success') return [];
    return Array.from(new Set(journalsState.data.journals.map((j) => j.specialty))).sort();
  }, [journalsState]);

  const rows = useMemo(() => {
    if (journalsState.status !== 'success') return [];
    const q = query.trim().toLowerCase();
    let filtered = journalsState.data.journals.filter((j) => {
      if (q && !j.name.toLowerCase().includes(q) && !j.ta.toLowerCase().includes(q)) return false;
      if (specialty !== 'all' && j.specialty !== specialty) return false;
      if (tier !== 'all' && String(j.tier) !== tier) return false;
      if (scope !== '' && j.scope !== scope) return false;
      return true;
    });
    filtered = filtered.slice().sort((a, b) => {
      const dir = sort.direction === 'asc' ? 1 : -1;
      const va = a[sort.column];
      const vb = b[sort.column];
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
      return String(va).localeCompare(String(vb)) * dir;
    });
    return filtered;
  }, [journalsState, query, specialty, tier, scope, sort]);

  function toggleSort(column: SortColumn) {
    setSort((prev) =>
      prev.column === column
        ? { column, direction: prev.direction === 'asc' ? 'desc' : 'asc' }
        : { column, direction: 'asc' },
    );
  }

  function sortButton(column: SortColumn, label: string) {
    const active = sort.column === column;
    return (
      <button
        type="button"
        onClick={() => toggleSort(column)}
        className="flex min-h-11 items-center gap-1 py-2 text-left text-xs font-semibold tracking-wide text-ink-muted uppercase hover:text-ink"
        aria-label={`Sort by ${label}${active ? `, currently ${sort.direction === 'asc' ? 'ascending' : 'descending'}` : ''}`}
      >
        {label}
        <span aria-hidden="true" className="text-ink-faint">
          {active ? (sort.direction === 'asc' ? '▲' : '▼') : ''}
        </span>
      </button>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-10 sm:px-6">
      <h1 className="text-3xl font-semibold">Sources</h1>
      <p className="mt-3 max-w-2xl text-ink-muted">
        Evidence Digest only draws from journals on this list, checked against PubMed by their
        MEDLINE title abbreviation. Nothing outside it appears in your feed. This is the whole
        basis for trusting the digest, so it's fully public.
      </p>

      <div className="mt-6 grid gap-3 rounded-lg border border-line bg-surface p-4 sm:grid-cols-3">
        {([1, 2, 3] as const).map((t) => (
          <div key={t}>
            <p className="text-sm font-semibold text-ink">{TIER_LABEL[t]}</p>
            <p className="mt-0.5 text-xs text-ink-muted">
              {t === 1 && 'Practice-defining, general-reach journals.'}
              {t === 2 && 'Widely read within their subspecialty.'}
              {t === 3 && 'Solid, established specialty journals.'}
            </p>
          </div>
        ))}
      </div>

      {journalsState.status === 'loading' && (
        <div className="mt-8 flex flex-col gap-2">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      )}

      {journalsState.status === 'error' && (
        <div className="mt-8">
          <ErrorState error={journalsState.error} onRetry={retry} />
        </div>
      )}

      {journalsState.status === 'success' && (
        <>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-end sm:flex-wrap">
            <div className="flex flex-col gap-1 sm:min-w-[14rem] sm:flex-1">
              <label htmlFor="journal-search" className="text-xs font-medium text-ink-muted">
                Search journals
              </label>
              <input
                id="journal-search"
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Name or abbreviation…"
                className="rounded-md border border-line-strong bg-surface px-3 py-2 text-sm text-ink"
              />
            </div>
            <Select
              id="journal-specialty"
              label="Specialty"
              value={specialty}
              onChange={setSpecialty}
              options={[
                { value: 'all', label: 'All specialties' },
                ...specialties.map((s) => ({ value: s, label: humanize(s) })),
              ]}
            />
            <Select
              id="journal-tier"
              label="Tier"
              value={tier}
              onChange={setTier}
              options={[
                { value: 'all', label: 'All tiers' },
                { value: '1', label: 'Tier 1' },
                { value: '2', label: 'Tier 2' },
                { value: '3', label: 'Tier 3' },
              ]}
            />
            <Select
              id="journal-scope"
              label="Coverage"
              value={scope}
              onChange={setScope}
              options={[
                { value: '', label: 'Any coverage' },
                { value: 'all', label: 'Every new article' },
                { value: 'topic', label: 'Topic-filtered' },
                { value: 'virus', label: 'Virus-focused' },
              ]}
            />
          </div>

          <p className="mt-3 text-sm text-ink-muted" aria-live="polite">
            {rows.length} of {journalsState.data.count} journals
          </p>

          <div className="mt-2 overflow-x-auto rounded-lg border border-line">
            <table className="w-full min-w-[640px] border-collapse text-sm">
              <caption className="sr-only">Journals watched by Evidence Digest</caption>
              <thead className="border-b border-line bg-[var(--color-surface-raised)]">
                <tr>
                  <th scope="col" className="px-3 text-left">
                    {sortButton('name', 'Journal')}
                  </th>
                  <th scope="col" className="px-3 text-left">
                    {sortButton('ta', 'MEDLINE abbreviation')}
                  </th>
                  <th scope="col" className="px-3 text-left">
                    {sortButton('specialty', 'Specialty')}
                  </th>
                  <th scope="col" className="px-3 text-left">
                    {sortButton('tier', 'Tier')}
                  </th>
                  <th scope="col" className="px-3 text-left">
                    {sortButton('scope', 'Coverage')}
                  </th>
                  <th scope="col" className="px-3 py-2 text-left text-xs font-semibold tracking-wide text-ink-muted uppercase">
                    PubMed
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((j) => (
                  <tr key={j.ta} className="border-b border-line last:border-0 even:bg-[var(--color-surface-raised)]">
                    <td className="px-3 py-2.5 font-medium text-ink">{j.name}</td>
                    <td className="px-3 py-2.5 text-ink-muted">{j.ta}</td>
                    <td className="px-3 py-2.5 text-ink-muted">{humanize(j.specialty)}</td>
                    <td className="px-3 py-2.5 text-ink-muted">{j.tier}</td>
                    <td className="px-3 py-2.5 text-ink-muted">
                      {j.scope === 'all' ? 'Every new article' : j.scope === 'virus' ? 'Virus-focused' : 'Topic-filtered'}
                    </td>
                    <td className="px-3 py-2.5">
                      <a
                        href={j.pubmedUrl}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="font-medium text-accent hover:underline"
                      >
                        View ↗
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {rows.length === 0 && (
            <p className="mt-6 text-center text-sm text-ink-muted">No journals match those filters.</p>
          )}
        </>
      )}
    </div>
  );
}
