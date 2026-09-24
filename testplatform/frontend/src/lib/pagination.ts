/**
 * Client-side pagination for tables that fetch every row and filter/sort in the browser
 * (RunHistoryTable). Paging happens AFTER filter + sort, so the filters, the sort and the
 * run count all still cover every run; only what is rendered is cut to one page.
 */

export const PAGE_SIZE_OPTIONS = [25, 50, 100, 250, 500] as const;
export const DEFAULT_PAGE_SIZE = 100;

export interface Page<T> {
  rows: T[];
  /** 1-based, clamped into [1, totalPages] -- a filter can shrink the list under a later page. */
  page: number;
  totalPages: number;
  /** 1-based index of the first row shown; 0 when there are no rows. */
  start: number;
  /** 1-based index of the last row shown; 0 when there are no rows. */
  end: number;
}

/** A persisted page size that is no longer offered (or corrupt) falls back to the default. */
export function normalizePageSize(size: unknown): number {
  return (PAGE_SIZE_OPTIONS as readonly number[]).includes(size as number)
    ? (size as number) : DEFAULT_PAGE_SIZE;
}

export function paginate<T>(rows: T[], page: number, pageSize: number): Page<T> {
  const size = normalizePageSize(pageSize);
  const totalPages = Math.max(1, Math.ceil(rows.length / size));
  const p = Math.min(Math.max(1, Math.floor(Number.isFinite(page) ? page : 1)), totalPages);
  const from = (p - 1) * size;
  const pageRows = rows.slice(from, from + size);
  return {
    rows: pageRows,
    page: p,
    totalPages,
    start: pageRows.length ? from + 1 : 0,
    end: from + pageRows.length,
  };
}
