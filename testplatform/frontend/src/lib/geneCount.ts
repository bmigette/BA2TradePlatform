// Mirrors backend strategy_param_space gene emission for a live UI preview.
export interface GeneInfo { name: string; choices: number; }
export interface GeneCount { genes: GeneInfo[]; searchSpace: number; }

interface Node { id: string; optimizeEnabled?: boolean; toggleOptimize?: boolean;
  valueMin?: number; valueMax?: number; valueStep?: number; conditions?: Node[]; }
interface Rule { id: string; conditions?: Node; actionValueOptimize?: boolean;
  actionValueMin?: number; actionValueMax?: number; actionValueStep?: number; toggleOptimize?: boolean;
  optionStrikeParamOptimize?: boolean; optionStrikeParamMin?: number; optionStrikeParamMax?: number; optionStrikeParamStep?: number;
  optionDteOptimize?: boolean; optionDteMinRange?: number; optionDteMaxRange?: number; optionDteStep?: number; }

const span = (mn?: number, mx?: number, st?: number): number => {
  if (mn == null || mx == null || !st || st <= 0 || mx < mn) return 1;
  return Math.floor((mx - mn) / st) + 1;
};
function walkNode(n: Node | undefined, out: GeneInfo[]): void {
  if (!n) return;
  for (const c of (n.conditions ?? [])) walkNode(c, out);
  if (n.optimizeEnabled) out.push({ name: `cond:${n.id}:value`, choices: span(n.valueMin, n.valueMax, n.valueStep) });
  if (n.toggleOptimize) out.push({ name: `cond:${n.id}:enabled`, choices: 2 });
}
export function countGenes(buyTree: Node | undefined, sellTree: Node | undefined, exitRules: Rule[]): GeneCount {
  const genes: GeneInfo[] = [];
  walkNode(buyTree, genes); walkNode(sellTree, genes);
  for (const r of (exitRules ?? [])) {
    walkNode(r.conditions, genes);
    if (r.actionValueOptimize) genes.push({ name: `exit:${r.id}:action_value`, choices: span(r.actionValueMin, r.actionValueMax, r.actionValueStep) });
    if (r.toggleOptimize) genes.push({ name: `exit:${r.id}:enabled`, choices: 2 });
    if (r.optionStrikeParamOptimize) genes.push({ name: `exit:${r.id}:option_delta`, choices: span(r.optionStrikeParamMin, r.optionStrikeParamMax, r.optionStrikeParamStep) });
    if (r.optionDteOptimize) genes.push({ name: `exit:${r.id}:option_dte`, choices: span(r.optionDteMinRange, r.optionDteMaxRange, r.optionDteStep) });
  }
  const searchSpace = genes.reduce((acc, g) => acc * Math.max(1, g.choices), 1);
  return { genes, searchSpace };
}
