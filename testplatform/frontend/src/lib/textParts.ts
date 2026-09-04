/**
 * Splitting a formatted rule line into plain and NUMERIC parts, so the reader's eye lands on
 * the thresholds rather than on the field names.
 *
 * The numbers are the part of a rule that the GA actually tuned and that differs between two
 * otherwise identical rules ("confidence >= 35" vs ">= 60"), so they carry the information;
 * `expected_profit` is just vocabulary.
 *
 * A digit only counts as a number when it does not continue an identifier: `atr_14` and
 * `sma200` are names, not thresholds, and bolding half of one reads as a typo.
 */
export type TextPart = { text: string; bold: boolean };

const NUMBER = /(?<![A-Za-z_0-9])[+-]?\d+(?:\.\d+)?%?/g;

export function boldNumbers(text: string): TextPart[] {
  if (!text) return [];
  const parts: TextPart[] = [];
  let last = 0;
  for (const m of text.matchAll(NUMBER)) {
    const at = m.index ?? 0;
    if (at > last) parts.push({ text: text.slice(last, at), bold: false });
    parts.push({ text: m[0], bold: true });
    last = at + m[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), bold: false });
  return parts;
}
