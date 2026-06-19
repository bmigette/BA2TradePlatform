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
  it('empty -> 0 genes, space 1', () => {
    const r = countGenes(undefined, undefined, []);
    expect(r.genes).toEqual([]); expect(r.searchSpace).toBe(1);
  });
});
