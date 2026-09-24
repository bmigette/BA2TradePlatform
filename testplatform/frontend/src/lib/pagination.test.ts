import { describe, it, expect } from 'vitest';
import { paginate, normalizePageSize, DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from './pagination';

const range = (n: number) => Array.from({ length: n }, (_, i) => i + 1);

describe('paginate', () => {
  it('defaults to 100 rows a page and offers up to 500', () => {
    expect(DEFAULT_PAGE_SIZE).toBe(100);
    expect(Math.max(...PAGE_SIZE_OPTIONS)).toBe(500);
  });

  it('cuts the requested page and reports its 1-based range', () => {
    const p = paginate(range(250), 2, 100);
    expect(p.rows[0]).toBe(101);
    expect(p.rows.length).toBe(100);
    expect([p.page, p.totalPages, p.start, p.end]).toEqual([2, 3, 101, 200]);
  });

  it('returns a short last page', () => {
    const p = paginate(range(250), 3, 100);
    expect([p.rows.length, p.start, p.end]).toEqual([50, 201, 250]);
  });

  it('clamps a page a filter has shrunk the list under', () => {
    // On page 5 of 500 runs, a filter leaves 30: show them, not an empty page 5.
    const p = paginate(range(30), 5, 25);
    expect([p.page, p.totalPages, p.start, p.end]).toEqual([2, 2, 26, 30]);
  });

  it('clamps below 1 and non-finite pages to the first page', () => {
    expect(paginate(range(10), 0, 25).page).toBe(1);
    expect(paginate(range(10), NaN, 25).page).toBe(1);
  });

  it('shows one empty page for no rows', () => {
    const p = paginate([], 3, 100);
    expect([p.rows.length, p.page, p.totalPages, p.start, p.end]).toEqual([0, 1, 1, 0, 0]);
  });
});

describe('normalizePageSize', () => {
  it('keeps an offered size and replaces anything else with the default', () => {
    expect(normalizePageSize(500)).toBe(500);
    expect(normalizePageSize(7)).toBe(DEFAULT_PAGE_SIZE);
    expect(normalizePageSize('100')).toBe(DEFAULT_PAGE_SIZE);
    expect(normalizePageSize(null)).toBe(DEFAULT_PAGE_SIZE);
  });
});
