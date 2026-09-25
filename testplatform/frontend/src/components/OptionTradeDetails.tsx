import React from 'react';
import { contractDetailRows, hasContractDetail } from '../lib/contractDetail';
import { moneyness, spotVsStrikePercent } from '../lib/optionTradeChart';
import { payoffFor, payoffSummary } from '../lib/optionChartView';
import type { HistoricalReference, TradeChartContext, TradeChartLeg } from '../lib/btApi';

/**
 * The option trade popup's detail panel: what happened, what the position is worth
 * at expiration, and each leg's terms with its moneyness at the recorded
 * references. Split out of the modal so the chart and the table can be reasoned
 * about (and failure-tested) separately.
 *
 * COLOUR DISCIPLINE: green and red mean position P&L and nothing else. Moneyness
 * badges are neutral, because ITM is not profitable -- a long call at $102 with a
 * $5 premium is ITM and 300 down.
 */

const money = (value: number | null | undefined) =>
  value == null ? '—' : `$${value.toFixed(2)}`;

const signed = (value: number | null | undefined) =>
  value == null ? '—' : `${value >= 0 ? '+' : '−'}$${Math.abs(value).toFixed(2)}`;

const qualityLabel: Record<HistoricalReference['quality'], string> = {
  recorded_snapshot: 'at fill',
  last_known_bar: 'last known bar',
  daily_reference: 'daily reference',
  unavailable: 'unavailable',
};

/**
 * One row of recorded contract detail. The mapping (dashes for not-recorded, the observation
 * label, the reason) lives in `lib/contractDetail` so it is testable without a browser.
 */
const ContractDetailRow: React.FC<{ row: ReturnType<typeof contractDetailRows>[number] }> = ({ row }) => (
  <tr className="border-t border-gray-100 dark:border-gray-700">
    <td className="py-1">{row.legLabel}</td>
    <td>{row.at}</td>
    <td className="font-mono text-[11px]">{row.asOf}</td>
    <td>{row.observation}</td>
    <td className="text-right">{row.iv}</td>
    <td className="text-right">{row.delta}</td>
    <td className="text-right">{row.gamma}</td>
    <td className="text-right">{row.theta}</td>
    <td className="text-right">{row.vega}</td>
    <td className="text-right">{row.openInterest}</td>
  </tr>
);

const MoneynessBadge: React.FC<{ leg: TradeChartLeg; reference: HistoricalReference }> = ({
  leg, reference,
}) => {
  const label = moneyness(leg.optionType, leg.strike, reference.price);
  if (label === 'unknown') {
    return <span className="text-xs text-gray-400 dark:text-gray-500">unknown</span>;
  }
  return (
    <span className="inline-flex flex-col">
      <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-200">
        {label}
      </span>
      <span className="text-[10px] text-gray-500 dark:text-gray-400">
        {money(reference.price)} · {qualityLabel[reference.quality]}
      </span>
    </span>
  );
};

const Card: React.FC<{ label: string; children: React.ReactNode; tone?: string }> = ({
  label, children, tone = '',
}) => (
  <div className={`rounded border border-gray-200 dark:border-gray-700 p-2 ${tone}`}>
    <div className="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{label}</div>
    <div className="text-sm font-semibold text-gray-900 dark:text-gray-100">{children}</div>
  </div>
);

const OptionTradeDetails: React.FC<{
  context: TradeChartContext;
  scope: number | null;
  onScopeChange: (scope: number | null) => void;
}> = ({ context, scope, onScopeChange }) => {
  const { legs } = context;
  const model = payoffFor(legs, scope ?? undefined);
  const underlying = context.underlying.symbol || '—';

  const entries = legs.map(leg => leg.entryAt).filter(Boolean).sort() as string[];
  const exits = legs.map(leg => leg.exitAt).filter(Boolean).sort() as string[];
  const recordedPnl = legs.reduce((total, leg) => total + (leg.pnl ?? 0), 0);
  // A leg with no recorded P&L used to contribute ZERO, which reads as "flat" and quietly
  // understates the total. It is counted instead, and the total says it is partial.
  const missingPnl = legs.filter(leg => leg.pnl == null).length;
  const openAtEnd = legs.some(leg => leg.positionStatus === 'open_at_end');
  // The premiums per share (one per leg) and the underlying beside them: a card that gives
  // only a date left the reader hunting through the leg table for what was actually paid
  // and received -- and for why an exit BELOW the strike can still be a winner.
  const premiums = (pick: (leg: typeof legs[number]) => number | null) =>
    legs.map(leg => money(pick(leg))).join(' / ');
  const spotAt = (pick: (leg: typeof legs[number]) => { price: number | null } | null | undefined) =>
    money(legs.map(leg => pick(leg)?.price).find(price => price != null) ?? null);

  return (
    <div className="space-y-3">
      {context.notices.length > 0 && (
        <ul className="space-y-1">
          {context.notices.map(notice => (
            <li key={notice.code}
                className="text-xs rounded bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200 px-2 py-1">
              {notice.message}
            </li>
          ))}
        </ul>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <Card label="Entry">
          <div>{entries[0] ? entries[0].slice(0, 16).replace('T', ' ') : '—'}</div>
          <div className="text-xs font-normal text-gray-700 dark:text-gray-300">
            premium {premiums(leg => leg.entryPrice)}/share · {underlying} {spotAt(leg => leg.entryUnderlying)}
          </div>
          <div className="text-[11px] font-normal text-gray-500">
            {legs.length > 1 ? 'first entry · average premiums' : 'recorded entry'}
          </div>
        </Card>
        <Card label={openAtEnd ? 'Marked at run end' : 'Exit'}>
          <div>{openAtEnd ? 'still open' : exits[exits.length - 1]?.slice(0, 16).replace('T', ' ') || '—'}</div>
          <div className="text-xs font-normal text-gray-700 dark:text-gray-300">
            {openAtEnd ? 'mark' : 'premium'} {premiums(leg => leg.exitPrice)}/share · {underlying} {spotAt(leg => leg.exitUnderlying)}
          </div>
          <div className="text-[11px] font-normal text-gray-500">
            {openAtEnd ? 'valuation, not a closing fill' : 'last exit'}
          </div>
        </Card>
        <Card label="Recorded P&L"
              tone={recordedPnl >= 0 ? 'bg-green-50 dark:bg-green-900/20' : 'bg-red-50 dark:bg-red-900/20'}>
          <div className={recordedPnl >= 0 ? 'text-green-700 dark:text-green-300' : 'text-red-700 dark:text-red-300'}>
            {signed(recordedPnl)}
          </div>
          <div className="text-[11px] font-normal text-gray-500">
            {openAtEnd ? 'marked, not realised' : 'stored result · includes recorded commissions'}
            {missingPnl > 0
              ? ` · ${missingPnl} leg${missingPnl === 1 ? '' : 's'} without a recorded P&L, so this total is partial`
              : ''}
          </div>
        </Card>
        <Card label="Contracts">
          <div>{legs.map(leg => leg.size ?? '—').join(' / ')}</div>
          <div className="text-[11px] font-normal text-gray-500">
            {legs.length} leg{legs.length === 1 ? '' : 's'} · multiplier{' '}
            {legs.map(leg => (leg.multiplierRecorded ? leg.multiplier ?? '—' : 'unrecorded')).join(' / ')}
          </div>
        </Card>
      </div>

      <div>
        <div className="flex items-center justify-between mb-1">
          <div className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            Expiration payoff · {underlying}
          </div>
          <select
            value={scope == null ? 'structure' : String(scope)}
            onChange={event => onScopeChange(event.target.value === 'structure' ? null : Number(event.target.value))}
            className="text-xs rounded border border-gray-300 dark:border-gray-600 bg-transparent px-2 py-1 text-gray-700 dark:text-gray-200">
            <option value="structure">Whole structure</option>
            {legs.map((leg, index) => (
              <option key={leg.id} value={index}>
                {`Leg ${index + 1}: ${leg.direction ?? '?'} ${leg.optionType ?? '?'} $${leg.strike ?? '?'}`}
              </option>
            ))}
          </select>
        </div>

        {!model.available ? (
          <div className="text-xs rounded bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 px-2 py-2">
            Payoff unavailable — {model.reason}
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Card label="Net entry">{payoffSummary(model).netEntryLabel}</Card>
            <Card label="Breakeven">{payoffSummary(model).breakevenLabel}</Card>
            <Card label="Max profit">{payoffSummary(model).maxProfit}</Card>
            <Card label="Max loss">{payoffSummary(model).maxLoss}</Card>
          </div>
        )}
        <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
          Hypothetical expiration payoff from recorded quantities and average entry premiums.
          Before fees; assumes vanilla contract payoff and no early exercise or assignment. This is
          not the recorded P&amp;L above.
        </p>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs [&_th]:px-2 [&_td]:px-2 [&_th]:whitespace-nowrap">
          <thead className="text-gray-500 dark:text-gray-400">
            <tr>
              <th className="text-left py-1">Leg</th>
              <th className="text-left">Contract</th>
              <th className="text-right">Strike</th>
              <th className="text-left">Expiry</th>
              <th className="text-right">Qty × mult.</th>
              <th className="text-right">Entry prem.</th>
              <th className="text-right">Exit prem.</th>
              <th className="text-left">At entry</th>
              <th className="text-left">At exit</th>
              <th className="text-right">P&amp;L</th>
              <th className="text-left">Exit reason</th>
            </tr>
          </thead>
          <tbody className="text-gray-800 dark:text-gray-200">
            {legs.map((leg, index) => (
              <tr key={leg.id}
                  className={`border-t border-gray-100 dark:border-gray-700 ${scope === index ? 'bg-cyan-50 dark:bg-cyan-900/20' : ''}`}>
                <td className="py-1">
                  <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-200">
                    {leg.direction === 'long' ? 'Long' : leg.direction === 'short' ? 'Short' : '—'}
                  </span>{' '}
                  {leg.optionType === 'call' ? 'Call' : leg.optionType === 'put' ? 'Put' : '—'}
                </td>
                <td className="font-mono text-[11px]">{leg.contractSymbol || '—'}</td>
                <td className="text-right">{money(leg.strike)}</td>
                <td>{leg.expiry || '—'}</td>
                <td className="text-right">
                  {leg.size ?? '—'} × {leg.multiplierRecorded ? leg.multiplier ?? '—' : 'unrecorded'}
                </td>
                <td className="text-right">{money(leg.entryPrice)}/share</td>
                <td className="text-right">{money(leg.exitPrice)}/share</td>
                <td><MoneynessBadge leg={leg} reference={leg.entryUnderlying} /></td>
                <td><MoneynessBadge leg={leg} reference={leg.exitUnderlying} /></td>
                <td className={`text-right ${(leg.pnl ?? 0) >= 0 ? 'text-green-700 dark:text-green-300' : 'text-red-700 dark:text-red-300'}`}>
                  {signed(leg.pnl)}
                </td>
                <td>{leg.exitReason || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {hasContractDetail(legs) && (
        <div>
          <div className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">
            Contract detail — as the option cache recorded it
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs [&_th]:px-2 [&_td]:px-2 [&_th]:whitespace-nowrap">
              <thead className="text-gray-500 dark:text-gray-400">
                <tr>
                  <th className="text-left py-1">Leg</th>
                  <th className="text-left">At</th>
                  <th className="text-left">As of</th>
                  <th className="text-left">Observation</th>
                  <th className="text-right">IV</th>
                  <th className="text-right">Δ</th>
                  <th className="text-right">Γ</th>
                  <th className="text-right">Θ</th>
                  <th className="text-right">V</th>
                  <th className="text-right">OI</th>
                </tr>
              </thead>
              <tbody className="text-gray-800 dark:text-gray-200">
                {contractDetailRows(legs).map(row => (
                  <ContractDetailRow key={row.key} row={row} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
            A daily bar is known only at its session close, so a timestamped event reads the
            last completed session before it — an approximation with a date, not an entry-time
            quote. A dash means NOT RECORDED, never zero.
 {contractDetailRows(legs)
   .filter(row => row.reason)
   .map(row => ` ${row.legLabel} at ${row.at}: ${row.reason}.`)
   .join('')}
          </p>
        </div>
      )}

      {legs.some(leg => leg.unavailableFields.length > 0) && (
        <p className="text-[11px] text-amber-700 dark:text-amber-300">
          Not recorded on some legs: {Array.from(new Set(legs.flatMap(leg => leg.unavailableFields))).join(', ')}.
          Missing terms are never substituted with a default.
        </p>
      )}

      <p className="text-[11px] text-gray-500 dark:text-gray-400">
        Moneyness uses the underlying reference recorded against each event
        {legs[0]?.entryUnderlying.quality === 'last_known_bar' ? ' (last completed session, not a fill-time quote)' : ''}.
        Spot vs strike is{' '}
        {legs[0] && legs[0].strike != null && legs[0].entryUnderlying.price != null
          ? `${(spotVsStrikePercent(legs[0].entryUnderlying.price, legs[0].strike) ?? 0).toFixed(2)}% at entry`
          : 'unavailable'}
        , not a return.
      </p>
    </div>
  );
};

export default OptionTradeDetails;
