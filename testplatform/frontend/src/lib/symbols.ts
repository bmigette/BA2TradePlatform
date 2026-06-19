/** Parse a free-text blob (.txt import / paste) into a clean symbol list:
 *  split on comma/whitespace/newline, uppercase, trim, dedup, drop empties. */
export function parseSymbols(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const s = raw.trim().toUpperCase();
    if (s && !seen.has(s)) { seen.add(s); out.push(s); }
  }
  return out;
}
