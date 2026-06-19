import { countGenes } from '../lib/geneCount';

export function GeneCountPreview({ buyTree, sellTree, exitRules }:
  { buyTree: any; sellTree: any; exitRules: any[] }) {
  const { genes, searchSpace } = countGenes(buyTree, sellTree, exitRules);
  const fmt = (n: number) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
  // suggested population hint: ~ clamp(genes*10, 30, 120)
  const pop = Math.min(120, Math.max(30, genes.length * 10));
  return (
    <div className="text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-700/40 border border-gray-200 dark:border-gray-700 rounded p-2 flex flex-wrap items-center gap-x-3 gap-y-1">
      <span><b className="text-gray-800 dark:text-gray-200">{genes.length}</b> optimizer gene{genes.length === 1 ? '' : 's'}</span>
      <span>~<b className="text-gray-800 dark:text-gray-200">{fmt(searchSpace)}</b> combinations</span>
      {genes.length > 0 && <span>suggest pop ≈ {pop}</span>}
      {genes.length === 0 && <span className="text-gray-400">no values marked Optimize yet</span>}
    </div>
  );
}
