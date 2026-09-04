/**
 * OPTION ROWS IN THE TRADE LIST (2026-09-04).
 *
 * The backtest recorder has always known what an option leg is; the trade list
 * simply never showed it, so a long call, a short put and one leg of a condor
 * all rendered as "SYMBOL, entry 4.20" -- which also reads exactly like a $4.20
 * stock, because the price columns carry the PREMIUM PER SHARE while the money
 * that moved is that premium times the contract multiplier.
 *
 * Pure, and in `lib/` rather than beside the page, so the arithmetic that
 * decides what a reader sees can be tested without mounting React.
 */

/** The option-specific fields a trade row may carry. Absent on every equity row. */
export type OptionTradeFields = {
  optionType?: 'call' | 'put' | null;
  strike?: number | null;
  expiry?: string | null;
  multiplier?: number | null;
  underlyingSymbol?: string | null;
  contractSymbol?: string | null;
};

/**
 * THE ONE DISCRIMINATOR. Every option-specific piece of chrome keys off this, so
 * the badge, the premium figure and the leg grouping cannot disagree about what
 * a row is. Deliberately NOT `!!contractSymbol` or a separate boolean the
 * backend could set inconsistently with the terms beside it.
 */
export const isOptionTrade = (t: OptionTradeFields | null | undefined): boolean =>
  !!t?.optionType;

/**
 * "C 150 · 2024-03-15" -- short enough to sit beside the symbol without taking a
 * column of its own; the table already carries twelve.
 *
 * A missing strike prints "?" rather than being dropped: "C · 2024-03-15" would
 * read as a complete description of a contract whose most important number is
 * missing.
 */
export const optionBadge = (t: OptionTradeFields | null | undefined): string => {
  if (!isOptionTrade(t)) return '';
  const right = t!.optionType === 'call' ? 'C' : 'P';
  const strike = t!.strike != null ? `${t!.strike}` : '?';
  const expiry = t!.expiry ? ` · ${t!.expiry}` : '';
  return `${right} ${strike}${expiry}`;
};

/**
 * The money that actually moved. The price columns hold the premium PER SHARE --
 * what the chain quotes -- while one contract controls `multiplier` (100)
 * shares, so the cash is premium x contracts x multiplier.
 *
 * `multiplier` falls back to 1, which is the equity no-op AND the right answer
 * for a blob written before the column existed. It is never assumed to be 100:
 * that would multiply every equity row by a hundred.
 */
export const contractValue = (premium: number, size: number,
                              multiplier?: number | null): number =>
  (Number(premium) || 0) * (Number(size) || 0) * (Number(multiplier) || 1);

/**
 * The structure a leg belongs to. Legs of one spread share a transactionId;
 * equity rows and single-leg options have none. Returned as a string because a
 * map key is one anyway and the id arrives as either type depending on which
 * engine wrote the blob.
 */
export const structureKey = (
  t: { transactionId?: number | string | null } | null | undefined,
): string | null => (t?.transactionId == null ? null : String(t.transactionId));

/** One row of the trade list: a plain trade, or a foldable multi-leg structure. */
export type TradeGroup<T> =
  | { kind: 'single'; key: string; trade: T }
  | { kind: 'structure'; key: string; legs: T[] };

/**
 * Fold legs that share a transactionId into one structure, PRESERVING THE ORDER
 * the caller sorted them into: a structure appears where its first leg would
 * have. Sorting the flat list and then grouping (rather than the other way
 * round) is what keeps "click P&L to sort" meaning the same thing whether or not
 * the run holds options.
 *
 * A LONE leg is NOT a structure. A single-leg option trade carries a
 * transactionId too, and wrapping it in a parent row would put a chevron on
 * every covered call hiding exactly one row -- noise, and one more click to
 * reach a number that was already on screen.
 */
export function groupTradesByStructure<T extends {
  id: string | number; transactionId?: number | string | null;
}>(trades: T[]): TradeGroup<T>[] {
  const byKey = new Map<string, T[]>();
  for (const trade of trades) {
    const key = structureKey(trade);
    if (key == null) continue;
    const bucket = byKey.get(key);
    if (bucket) bucket.push(trade); else byKey.set(key, [trade]);
  }
  const emitted = new Set<string>();
  const out: TradeGroup<T>[] = [];
  for (const trade of trades) {
    const key = structureKey(trade);
    const legs = key == null ? undefined : byKey.get(key);
    if (key == null || !legs || legs.length < 2) {
      out.push({ kind: 'single', key: `t${trade.id}`, trade });
      continue;
    }
    if (emitted.has(key)) continue;
    emitted.add(key);
    out.push({ kind: 'structure', key: `s${key}`, legs });
  }
  return out;
}

export type StructureLeg = {
  entryDate: string; exitDate: string; entryPrice: number; exitPrice: number;
  size: number; direction: 'long' | 'short'; pnl: number; pnlPercent: number;
  exitReason: string; multiplier?: number | null;
  underlyingSymbol?: string | null; symbol?: string;
};

/**
 * What a whole structure did, from its legs.
 *
 * Money is NET and signed from the ACCOUNT's point of view: a long leg PAID its
 * premium (debit) and a short leg RECEIVED it (credit), so a net cost above zero
 * is a debit spread and below zero a credit one -- and `netValue - netCost` is
 * then the P&L, which is what makes the two columns readable side by side.
 *
 * The per-share premium is deliberately NOT summed across legs: two legs at 4.20
 * and 1.10 do not make 5.30 of anything, and a column that adds them invents a
 * number. The structure row shows MONEY; the per-share premiums stay on the
 * legs, where they are the quote the chain actually published.
 */
export function summariseStructure<T extends StructureLeg>(legs: T[]) {
  const sign = (leg: T) => (leg.direction === 'long' ? 1 : -1);
  const netCost = legs.reduce(
    (acc, leg) => acc + sign(leg) * contractValue(leg.entryPrice, leg.size, leg.multiplier), 0);
  const netValue = legs.reduce(
    (acc, leg) => acc + sign(leg) * contractValue(leg.exitPrice, leg.size, leg.multiplier), 0);
  const reasons = Array.from(new Set(legs.map(l => l.exitReason).filter(Boolean)));
  const entryDates = legs.map(l => l.entryDate).filter(Boolean).sort();
  const exitDates = legs.map(l => l.exitDate).filter(Boolean).sort();
  return {
    // The UNDERLYING, not the OCC string: four legs of one condor share it, and
    // it is what the reader recognises.
    symbol: legs[0]?.underlyingSymbol || legs[0]?.symbol || '',
    legCount: legs.length,
    entryDate: entryDates[0] || '',
    exitDate: exitDates[exitDates.length - 1] || '',
    netCost,
    netValue,
    contracts: legs.reduce((acc, leg) => acc + (Number(leg.size) || 0), 0),
    pnl: legs.reduce((acc, leg) => acc + (Number(leg.pnl) || 0), 0),
    // SUMMED, like the P&L it is the percentage of: every leg's pnlPercent is a
    // share of the SAME account equity at entry, so they add. Averaging them
    // would report a two-leg bet as half the account impact it had.
    pnlPercent: legs.reduce((acc, leg) => acc + (Number(leg.pnlPercent) || 0), 0),
    // One reason if the legs agree, else say how many rather than pick one.
    exitReason: reasons.length === 1 ? reasons[0] : `${reasons.length} reasons`,
    // The SHAPE of the structure, which a single long/short badge cannot say.
    longLegs: legs.filter(l => l.direction === 'long').length,
    shortLegs: legs.filter(l => l.direction === 'short').length,
  };
}
