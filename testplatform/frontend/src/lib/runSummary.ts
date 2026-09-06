// What a backtest row can say about itself BEYOND the columns on screen.
//
// The list endpoint already returns profit factor, the winning/losing split, best/worst trade,
// final equity, average duration, the window, the fitness metric and the timestamps -- none of
// which fit as columns, and all of which are already in memory. So the hover card costs no
// request: it reads the row the table is already holding.
//
// It also resolves the camelCase/snake_case duality in ONE place. The API answers camelCase but
// several callers still send snake, so the table reads `r.totalReturn ?? r.total_return` at every
// single cell; a field added on one spelling and read on the other silently renders "—".

/** A row as the list endpoint returns it. Deliberately loose: the shape is the API's, not ours. */
export type RunRow = Record<string, any>;

export interface SummaryField {
  label: string;
  value: string;
}

/** First non-nullish of the camel/snake spellings. `null` and `undefined` only -- never 0 or ''. */
export function pick(row: RunRow | null | undefined, ...names: string[]): any {
  if (!row) return undefined;
  for (const n of names) {
    const v = row[n];
    if (v !== null && v !== undefined) return v;
  }
  return undefined;
}

const num = (v: any): number | undefined => {
  const n = typeof v === 'string' ? Number(v) : v;
  return typeof n === 'number' && Number.isFinite(n) ? n : undefined;
};

const money = (v: any): string | undefined => {
  const n = num(v);
  return n === undefined ? undefined
    : n.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
};

const pct = (v: any, digits = 1): string | undefined => {
  const n = num(v);
  return n === undefined ? undefined : `${n.toFixed(digits)}%`;
};

const fixed = (v: any, digits = 2): string | undefined => {
  const n = num(v);
  return n === undefined ? undefined : n.toFixed(digits);
};

const day = (v: any): string | undefined => {
  if (!v) return undefined;
  const s = String(v);
  return s.length >= 10 ? s.slice(0, 10) : s;
};

/**
 * Ordered fields for the hover card, in the order a reader wants them.
 *
 * A field the row cannot answer is OMITTED rather than rendered as "—" or 0: a card of dashes
 * teaches the reader to stop looking at it, and a fabricated 0 in a money row is the failure this
 * codebase keeps guarding against. An unfinished run therefore shows a short card, which is
 * itself the honest signal.
 */
export function summariseRun(row: RunRow | null | undefined): SummaryField[] {
  if (!row) return [];
  const out: SummaryField[] = [];
  const add = (label: string, value: string | undefined) => {
    if (value !== undefined && value !== '') out.push({ label, value });
  };

  const wins = num(pick(row, 'winningTrades', 'winning_trades'));
  const losses = num(pick(row, 'losingTrades', 'losing_trades'));

  add('Status', pick(row, 'status'));
  add('Engine', pick(row, 'engineType', 'engine_type'));
  add('Fitness', pick(row, 'fitnessMetric', 'fitness_metric'));

  const start = day(pick(row, 'startDate', 'start_date'));
  const end = day(pick(row, 'endDate', 'end_date'));
  if (start && end) add('Window', `${start} → ${end}`);

  add('Profit factor', fixed(pick(row, 'profitFactor', 'profit_factor')));
  if (wins !== undefined && losses !== undefined) add('Win / loss', `${wins} / ${losses}`);
  add('Best trade', money(pick(row, 'bestTrade', 'best_trade')));
  add('Worst trade', money(pick(row, 'worstTrade', 'worst_trade')));
  add('Avg duration', (() => {
    const d = num(pick(row, 'avgTradeDuration', 'avg_trade_duration'));
    return d === undefined ? undefined : `${d.toFixed(1)} d`;
  })());

  add('Initial capital', money(pick(row, 'initialCapital', 'initial_capital')));
  add('Final equity', money(pick(row, 'finalEquity', 'final_equity')));

  // The visible columns, repeated ONLY where the card adds precision the column rounds away.
  add('Return', pct(pick(row, 'totalReturn', 'total_return'), 2));
  add('Max drawdown', pct(pick(row, 'maxDrawdown', 'max_drawdown'), 2));
  add('Win rate', pct(pick(row, 'winRate', 'win_rate'), 2));

  add('Created', day(pick(row, 'createdAt', 'created_at')));
  add('Error', pick(row, 'errorMessage', 'error_message'));
  return out;
}

/** Every label on the row, as strings. The cell truncates at two lines; the card does not. */
export function runLabels(row: RunRow | null | undefined): string[] {
  const raw = pick(row, 'labels');
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.map(String) : [raw];
    } catch {
      return [raw];
    }
  }
  return [];
}

/**
 * Where to put a card of ``w``x``h`` for a pointer at (x, y), inside a ``vw``x``vh`` viewport.
 *
 * Pure so the flipping is testable: a card that opens off-screen at the bottom of a long table is
 * the whole reason a native ``title`` was tolerable in the first place.
 */
export function cardPosition(
  x: number, y: number, w: number, h: number, vw: number, vh: number, gap = 14,
): { left: number; top: number } {
  const left = x + gap + w > vw ? Math.max(gap, x - gap - w) : x + gap;
  const top = y + gap + h > vh ? Math.max(gap, vh - h - gap) : y + gap;
  return { left, top };
}
