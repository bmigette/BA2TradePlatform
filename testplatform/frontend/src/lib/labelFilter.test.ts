import { describe, it, expect } from 'vitest';
import { matchesLabels } from './labelFilter';

/**
 * Asked for from live use on 2026-09-04: the Saved tab's label chips were AND-only, so
 * "show me S1, S2 and S3" was impossible -- selecting three strategy tags asked for runs
 * carrying all three, which no run does.
 */

const run = (...labels: string[]) => labels;

describe('matchesLabels', () => {
  it('lets everything through when nothing is selected', () => {
    // An empty selection means the filter is OFF, not "match no labels".
    expect(matchesLabels(run('goal6'), [], 'all')).toBe(true);
    expect(matchesLabels(run('goal6'), [], 'any')).toBe(true);
    expect(matchesLabels([], [], 'all')).toBe(true);
  });

  describe('all', () => {
    it('requires every selected label', () => {
      expect(matchesLabels(run('goal2020-notional', 'S4'), ['goal2020-notional', 'S4'], 'all')).toBe(true);
      expect(matchesLabels(run('goal2020-notional'), ['goal2020-notional', 'S4'], 'all')).toBe(false);
    });

    it('ignores extra labels on the run', () => {
      expect(matchesLabels(run('goal6', 'S4', 'risk_atr'), ['goal6'], 'all')).toBe(true);
    });
  });

  describe('any', () => {
    it('accepts a run carrying just one of the selected labels', () => {
      // The case that motivated the toggle: three strategy tags no single run can all carry.
      expect(matchesLabels(run('S2'), ['S1', 'S2', 'S3'], 'any')).toBe(true);
      expect(matchesLabels(run('S3'), ['S1', 'S2', 'S3'], 'any')).toBe(true);
    });

    it('still rejects a run carrying none of them', () => {
      expect(matchesLabels(run('S7', 'goal6'), ['S1', 'S2', 'S3'], 'any')).toBe(false);
    });

    it('is a strict superset of all for the same selection', () => {
      const labels = run('goal2020-notional', 'S4');
      const selected = ['goal2020-notional', 'S4'];
      expect(matchesLabels(labels, selected, 'all')).toBe(true);
      expect(matchesLabels(labels, selected, 'any')).toBe(true);
    });
  });

  it('treats a missing or malformed labels field as no labels', () => {
    // Older rows predate the labels column; they must not crash the filter.
    expect(matchesLabels(undefined, ['S1'], 'all')).toBe(false);
    expect(matchesLabels(null, ['S1'], 'any')).toBe(false);
    expect(matchesLabels('S1', ['S1'], 'any')).toBe(false);
    expect(matchesLabels(undefined, [], 'all')).toBe(true);
  });
});
