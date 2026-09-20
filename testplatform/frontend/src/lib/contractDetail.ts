/**
 * Contract detail (greeks / IV / open interest) for the option popup's leg rows.
 *
 * Spec 2026-09-20, review finding R6(3). The API already returned `entryContract` /
 * `exitContract` and NOTHING consumed them: the browser fixture supplied real-looking IV,
 * delta, gamma, theta, vega, volume and OI and the popup displayed none of it.
 *
 * This module is the consumption point, and it is pure so the mapping can be asserted without
 * a browser:
 *
 *  * a null greek is NOT RECORDED and renders as "—", never as zero (the cache migration left
 *    older rows null deliberately);
 *  * the as-of date travels with every row, because an "approximate prior session" reading is
 *    a DIFFERENT observation from an entry-time quote;
 *  * the reason travels too, so an unavailable row explains itself instead of showing dashes.
 */
import type { ContractDetail, TradeChartLeg } from './btApi';

export const contractQualityLabel: Record<ContractDetail['quality'], string> = {
  cache_bar: 'own session',
  approximate_prior_session: 'prior session (approximate)',
  daily_reference: 'daily reference',
  partial: 'bar without greeks',
  unavailable: 'unavailable',
};

/** A greek as text: a dash for "not recorded", fixed digits otherwise. */
export const greekText = (value: number | null | undefined, digits = 3): string =>
  value == null || Number.isNaN(value) ? '—' : value.toFixed(digits);

export type ContractDetailRow = {
  key: string;
  legLabel: string;
  at: 'entry' | 'exit';
  asOf: string;
  observation: string;
  iv: string;
  delta: string;
  gamma: string;
  theta: string;
  vega: string;
  openInterest: string;
  reason: string | null;
  supplied: boolean;
};

export const contractDetailRows = (legs: TradeChartLeg[]): ContractDetailRow[] =>
  legs.flatMap((leg, index) => {
    const legLabel =
      `${index + 1}: ${leg.direction ?? '?'} ${leg.optionType ?? '?'} $${leg.strike ?? '?'}`;
    const build = (which: 'entry' | 'exit'): ContractDetailRow => {
      const detail = which === 'entry' ? leg.entryContract : leg.exitContract;
      if (!detail) {
        return {
          key: `${leg.id}-${which}`, legLabel, at: which, asOf: '—', observation: 'not supplied',
          iv: '—', delta: '—', gamma: '—', theta: '—', vega: '—', openInterest: '—',
          reason: null, supplied: false,
        };
      }
      return {
        key: `${leg.id}-${which}`,
        legLabel,
        at: which,
        asOf: detail.asOf || '—',
        observation: contractQualityLabel[detail.quality],
        iv: greekText(detail.iv),
        delta: greekText(detail.delta),
        gamma: greekText(detail.gamma, 4),
        theta: greekText(detail.theta, 4),
        vega: greekText(detail.vega, 4),
        openInterest: detail.openInterest == null ? '—' : String(detail.openInterest),
        reason: detail.reason ?? null,
        supplied: true,
      };
    };
    return [build('entry'), build('exit')];
  });

/** True when any leg carries contract detail worth rendering at all. */
export const hasContractDetail = (legs: TradeChartLeg[]): boolean =>
  legs.some(leg => Boolean(leg.entryContract || leg.exitContract));
