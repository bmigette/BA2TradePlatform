import { describe, it, expect } from 'vitest';
import { countGenes } from './geneCount';
describe('countGenes', () => {
  it('counts cond value + rule toggle + action_value genes and search space', () => {
    const exit = [{ id: 'r1', toggleOptimize: true,
      conditions: { id: 'g', conditions: [{ id: 'c1', optimizeEnabled: true, valueMin: 0, valueMax: 10, valueStep: 2 }] },
      actionValueOptimize: true, actionValueMin: -10, actionValueMax: 0, actionValueStep: 5 }];
    const r = countGenes(undefined, undefined, exit as any);
    const names = r.genes.map(g => g.name).sort();
    expect(names).toEqual(['cond:c1:value', 'exit:r1:action_value', 'exit:r1:enabled']);
    // choices: c1 value = floor(10/2)+1=6 ; action_value = floor(10/5)+1=3 ; enabled=2  => 6*3*2 = 36
    expect(r.searchSpace).toBe(36);
  });
  // Mode genes (design 2026-09-15 section 5). The displayed count has to equal what
  // strategy_param_space emits, or the editor advertises a search space no run can have.
  it('counts a mode gene beside the threshold gene of a NUMERIC mode leaf', () => {
    const exit = [{ id: 'r1',
      conditions: { id: 'g', conditions: [{ id: 'o_lc-market-adx', optimizeEnabled: true,
        valueMin: 10, valueMax: 40, valueStep: 5,
        modeOptimize: true, modeChoices: ['off', 'below', 'above'] }] } }];
    const r = countGenes(undefined, undefined, exit as any);
    expect(r.genes.map(g => g.name).sort())
      .toEqual(['cond:o_lc-market-adx:mode', 'cond:o_lc-market-adx:value']);
    // 7 thresholds x 3 modes
    expect(r.searchSpace).toBe(21);
  });
  it('counts ONLY the mode gene for a CATEGORICAL mode leaf (it has no threshold)', () => {
    const exit = [{ id: 'r1',
      conditions: { id: 'g', conditions: [{ id: 'o_lc-market-structure-state',
        optimizeEnabled: true, modeOptimize: true, modeChoices: ['off', 'bull', 'bear'] }] } }];
    const r = countGenes(undefined, undefined, exit as any);
    expect(r.genes).toEqual([{ name: 'cond:o_lc-market-structure-state:mode', choices: 3 }]);
    expect(r.searchSpace).toBe(3);
  });
  it('empty -> 0 genes, space 1', () => {
    const r = countGenes(undefined, undefined, []);
    expect(r.genes).toEqual([]); expect(r.searchSpace).toBe(1);
  });
});
