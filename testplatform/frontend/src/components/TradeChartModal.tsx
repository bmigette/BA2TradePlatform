import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  createChart, ColorType, CandlestickSeries, LineStyle, createSeriesMarkers,
} from 'lightweight-charts';
import type { IChartApi, ISeriesApi, CandlestickData, Time, SeriesMarker } from 'lightweight-charts';
import { getOhlcvBars, getTradeChartContext } from '../lib/btApi';
import type { TradeChartContext } from '../lib/btApi';
import {
  allMarkers, payoffFor, payoffGeometry, payoffSummary, pnlTicks, strikeLineLegs, strikeLines,
  zoneBands,
} from '../lib/optionChartView';
import OptionTradeDetails from './OptionTradeDetails';

/** The subset of a backtest Trade this modal needs. */
export interface TradeLike {
  symbol?: string;
  entryDate: string;
  exitDate: string;
  entryPrice: number;
  exitPrice: number;
  direction: 'long' | 'short';
  pnl: number;
  pnlPercent: number;
  exitReason: string;
}

const dayOf = (iso?: string) => (iso || '').slice(0, 10); // ISO datetime -> 'YYYY-MM-DD'
const addDays = (iso: string, n: number) => {
  const d = new Date(dayOf(iso) + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};

const CHART_HEIGHT = 380;
/** Width of the payoff panel beside the chart. The payoff used to be drawn OVER the candles,
 *  rotated, so the horizontal axis meant time for the candles and dollars for the curve at
 *  once, and its $0 line read as a date. It now has its own strip, sharing only the price
 *  axis (the one thing the two genuinely have in common). */
const PAYOFF_PANEL_WIDTH = 200;

/**
 * A load, keyed by WHAT WAS ASKED FOR.
 *
 * Loading and error are DERIVED from the key rather than stored: an effect that
 * synchronously resets four pieces of state before fetching is both a cascade of
 * renders and a window in which stale bars sit under a new trade's header. Here a
 * response that does not match the current key simply is not rendered.
 */
type Loaded<T> = { key: string; value: T; error: string | null };

/** Zone bands over the candles (``width``) and the payoff panel beside them
 *  (``panelWidth``: ``zeroX``, ``curve``, ``fills`` and ``ticks`` are panel coordinates). */
type OverlayShape = {
  width: number;
  panelWidth: number;
  height: number;
  zeroX: number;
  curve: string;
  fills: Array<{ sign: 'profit' | 'loss'; path: string }>;
  bandRects: Array<{ y: number; height: number; sign: 'profit' | 'loss' | 'neutral' }>;
  /** The horizontal P&L scale the locked design puts along the top (decision 1a). */
  ticks: Array<{ x: number; label: string }>;
};

/**
 * Click-through chart for one backtest trade.
 *
 * STOCK rows keep the original view: daily candles around the trade window with an
 * entry and an exit marker, bars from `/tools/ohlcv/bars`.
 *
 * OPTION rows (spec 2026-09-20, decisions 1a/2c/3) get the option view: the
 * underlying's CACHED daily bars, one dashed line per strike, BOTH marker sets with
 * a toggle, and the expiration payoff drawn as a rotated overlay on the price axis
 * — plus the leg table and the payoff figures underneath. Its data comes from the
 * cache-only `/backtests/{id}/trade-chart` endpoint, so opening it never fetches
 * bars from a provider.
 */
const TradeChartModal: React.FC<{
  trade: TradeLike | null;
  optionSelection?: { backtestId: number; tradeId: number } | null;
  onClose: () => void;
}> = ({ trade, optionSelection, onClose }) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);

  const [stockLoad, setStockLoad] = useState<Loaded<CandlestickData[]> | null>(null);
  const [optionLoad, setOptionLoad] = useState<Loaded<TradeChartContext | null> | null>(null);
  const [scope, setScope] = useState<number | null>(null);
  const [showStructureMarkers, setShowStructureMarkers] = useState(true);
  const [showLegMarkers, setShowLegMarkers] = useState(true);
  const [showOverlay, setShowOverlay] = useState(true);
  const [overlay, setOverlay] = useState<OverlayShape | null>(null);
  // Bumped whenever the chart pans, zooms or resizes: the overlay is positioned in
  // pixels, so it must be recomputed from the chart's own price scale.
  const [viewport, setViewport] = useState(0);

  const isOptionView = !!optionSelection;
  const symbol = trade?.symbol;
  const entryDate = trade?.entryDate;
  const exitDate = trade?.exitDate;

  const stockKey = !isOptionView && symbol
    ? `${symbol}|${dayOf(entryDate)}|${dayOf(exitDate)}` : null;
  const optionKey = optionSelection
    ? `${optionSelection.backtestId}:${optionSelection.tradeId}` : null;

  // ---- STOCK: bars from the provider-backed endpoint (unchanged behaviour) -----
  useEffect(() => {
    if (!stockKey || !symbol || !entryDate) return;
    let alive = true;
    const start = addDays(entryDate, -20);
    const end = addDays(exitDate || entryDate, 20);
    getOhlcvBars(symbol, start, end, '1d')
      .then(res => {
        if (!alive) return;
        setStockLoad({
          key: stockKey, error: null,
          value: (res.bars || []).map(b => ({
            time: dayOf(b.Date) as Time, open: b.Open, high: b.High, low: b.Low, close: b.Close,
          })),
        });
      })
      .catch(e => {
        if (alive) setStockLoad({ key: stockKey, value: [], error: String(e) });
      });
    return () => { alive = false; };
  }, [stockKey, symbol, entryDate, exitDate]);

  // ---- OPTION: the complete transaction, cache-only ----------------------------
  // Keyed on the two ids, NOT on the ``optionSelection`` object: the page hands over a fresh
  // object on every re-render (its running-jobs poll re-renders it every few seconds), and an
  // object dependency re-fetched the trade each time -- and ``setScope(null)`` below then
  // snapped the payoff selector back to "Whole structure" moments after a leg was picked.
  const optionBacktestId = optionSelection?.backtestId;
  const optionTradeId = optionSelection?.tradeId;
  useEffect(() => {
    if (!optionKey || optionBacktestId == null || optionTradeId == null) return;
    let alive = true;
    getTradeChartContext(optionBacktestId, optionTradeId)
      .then(res => {
        if (!alive) return;
        setOptionLoad({ key: optionKey, value: res, error: null });
        setScope(null);
      })
      .catch(e => {
        if (alive) setOptionLoad({ key: optionKey, value: null, error: String(e) });
      });
    return () => { alive = false; };
  }, [optionKey, optionBacktestId, optionTradeId]);

  const stockReady = stockLoad && stockLoad.key === stockKey ? stockLoad : null;
  const optionReady = optionLoad && optionLoad.key === optionKey ? optionLoad : null;

  const bars = stockReady?.value ?? [];
  const context = optionReady?.value ?? null;
  const error = isOptionView ? optionReady?.error ?? null : stockReady?.error ?? null;
  const loading = isOptionView ? !optionReady : !stockReady;

  const optionBars = useMemo<CandlestickData[]>(() => {
    if (!context) return [];
    return context.underlying.bars
      .filter(bar => bar.open != null && bar.high != null && bar.low != null && bar.close != null)
      .map(bar => ({
        time: bar.date as Time,
        open: bar.open as number, high: bar.high as number,
        low: bar.low as number, close: bar.close as number,
      }));
  }, [context]);

  const effectiveScope = scope != null && context && scope < context.legs.length ? scope : null;
  const payoff = useMemo(
    () => (context ? payoffFor(context.legs, effectiveScope ?? undefined) : null),
    [context, effectiveScope],
  );

  const data = isOptionView ? optionBars : bars;
  const dataReady = data.length > 0;

  // ---- build the chart ---------------------------------------------------------
  useEffect(() => {
    if (!trade || !containerRef.current || data.length === 0) return;

    const isDark = document.documentElement.classList.contains('dark');
    const chart: IChartApi = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: CHART_HEIGHT,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: isDark ? '#cbd5e1' : '#334155' },
      grid: { vertLines: { color: isDark ? '#1f2937' : '#eef2f7' }, horzLines: { color: isDark ? '#1f2937' : '#eef2f7' } },
      timeScale: { borderColor: isDark ? '#334155' : '#cbd5e1' },
      rightPriceScale: { borderColor: isDark ? '#334155' : '#cbd5e1' },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#16a34a', downColor: '#dc2626', borderVisible: false,
      wickUpColor: '#16a34a', wickDownColor: '#dc2626',
    });
    series.setData(data);
    seriesRef.current = series;

    const markers: SeriesMarker<Time>[] = [];
    if (isOptionView && context) {
      const priceRange = {
        min: Math.min(...data.map(bar => bar.low as number)),
        max: Math.max(...data.map(bar => bar.high as number)),
      };
      for (const line of strikeLines(strikeLineLegs(context.legs), priceRange)) {
        series.createPriceLine({
          price: line.price,
          color: line.collidesWithPrevious ? '#94a3b8' : '#64748b',
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: line.label,
        });
      }
      // The line the green/red zones meet at. Without it the zones read as "profit / no
      // profit" for the TRADE, and an early exit that sat in the red zone -- sold for more
      // than it cost, weeks before expiry -- looked impossible. Labelled as what it is.
      if (showOverlay && payoff?.available) {
        for (const breakeven of payoff.breakevens) {
          if (!Number.isFinite(breakeven)) continue;
          series.createPriceLine({
            price: breakeven,
            color: '#eab308',
            lineWidth: 1,
            lineStyle: LineStyle.Dotted,
            axisLabelVisible: true,
            title: `Breakeven at expiry $${breakeven.toFixed(2)}`,
          });
        }
      }
      for (const marker of allMarkers(context.legs, {
        structure: showStructureMarkers, legs: showLegMarkers,
      })) {
        markers.push({
          time: marker.time as Time,
          position: marker.position,
          color: marker.color,
          shape: marker.shape,
          // Short on the canvas, deliberately: a bar marker has no tooltip in this version of
          // lightweight-charts (`SeriesMarkerBar` carries no `title`), so the long labels that
          // used to be painted here overlapped into an unreadable smear. The detail lives in
          // the leg table beside the chart -- the "linked panel" the review asked for.
          text: marker.text,
        });
      }
    } else {
      markers.push({
        time: dayOf(trade.entryDate) as Time, position: 'belowBar', color: '#2563eb',
        shape: 'arrowUp', text: `Entry $${trade.entryPrice.toFixed(2)}`,
      });
      if (trade.exitDate) {
        markers.push({
          time: dayOf(trade.exitDate) as Time, position: 'aboveBar',
          color: trade.pnl >= 0 ? '#16a34a' : '#dc2626', shape: 'arrowDown',
          text: `Exit $${trade.exitPrice.toFixed(2)}`,
        });
      }
    }
    if (markers.length > 0) createSeriesMarkers(series, markers);

    chart.timeScale().fitContent();

    const recompute = () => setViewport(value => value + 1);
    chart.timeScale().subscribeVisibleLogicalRangeChange(recompute);
    const onResize = () => {
      if (!containerRef.current) return;
      chart.applyOptions({ width: containerRef.current.clientWidth });
      recompute();
    };
    window.addEventListener('resize', onResize);

    // A vertical PRICE-axis drag moves every candle and strike line without firing the
    // time-range event or a resize, so the SVG overlay kept the projection it was built with
    // and its strike kinks and zero crossing pointed at the wrong underlying prices (review
    // R4 -- a calculation error, not a cosmetic one).
    //
    // The library emits no "price scale moved" event, so the projection is WATCHED instead:
    // two known prices are sampled every frame, and when their screen coordinates (or the
    // scale's width) change, the scale moved and the overlay is rebuilt from the new one.
    const probes = [
      data.length > 0 ? data[data.length - 1].close : 0,
      data.reduce((lowest, bar) => Math.min(lowest, bar.low), Number.POSITIVE_INFINITY),
    ].filter(value => Number.isFinite(value));
    let signature = '';
    let frame = 0;
    const watchProjection = () => {
      const current = [
        ...probes.map(price => series.priceToCoordinate(price)),
        series.priceScale().width(),
      ].join('|');
      if (current !== signature) {
        signature = current;
        recompute();
      }
      frame = requestAnimationFrame(watchProjection);
    };
    if (isOptionView && probes.length > 0) frame = requestAnimationFrame(watchProjection);

    recompute();

    return () => {
      if (frame) cancelAnimationFrame(frame);
      window.removeEventListener('resize', onResize);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(recompute);
      seriesRef.current = null;
      chart.remove();
    };
  }, [trade, data, isOptionView, context, showStructureMarkers, showLegMarkers, showOverlay, payoff]);

  // ---- position the rotated payoff overlay ------------------------------------
  useEffect(() => {
    const series = seriesRef.current;
    const container = containerRef.current;
    if (!series || !container || !isOptionView || !payoff || !payoff.available) {
      setOverlay(null);
      return;
    }
    const width = container.clientWidth;
    if (width <= 0) return;

    const geometry = payoffGeometry(payoff);
    const plotWidth = width - series.priceScale().width();
    // The panel: P&L runs outwards from a vertical $0 line in its middle, using the whole
    // strip (less a margin for the curve's stroke). Heights come from the chart's own price
    // projection, so a price means the same height on both sides.
    const panelWidth = PAYOFF_PANEL_WIDTH;
    const zeroX = panelWidth / 2;
    const halfWidth = panelWidth / 2 - 8;
    const scale = geometry.maxAbsPnl > 0 ? halfWidth / geometry.maxAbsPnl : 0;

    // `priceToCoordinate` returns a branded Coordinate: unwrap it to a plain number
    // so the paths are ordinary arithmetic and the filters below narrow.
    const project = (point: { underlying: number; pnl: number }): { x: number; y: number } | null => {
      const y = series.priceToCoordinate(point.underlying);
      if (y == null) return null;
      return { x: zeroX + point.pnl * scale, y: Number(y) };
    };

    const defined = geometry.points
      .map(project)
      .filter((point): point is { x: number; y: number } => point != null);
    if (defined.length < 2) { setOverlay(null); return; }

    const toPath = (points: Array<{ x: number; y: number }>) =>
      points
        .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`)
        .join(' ');

    const fills: OverlayShape['fills'] = [];
    for (const segment of geometry.segments) {
      const projected = segment.points
        .map(project)
        .filter((point): point is { x: number; y: number } => point != null);
      if (projected.length >= 2) {
        const first = projected[0];
        const last = projected[projected.length - 1];
        // Close the area back along the zero line so the fill reads as P&L from zero.
        fills.push({
          sign: segment.sign,
          path: `${toPath(projected)} L${zeroX.toFixed(1)},${last.y.toFixed(1)} L${zeroX.toFixed(1)},${first.y.toFixed(1)} Z`,
        });
      }
    }

    // The P&L scale along the top: without it the reader cannot tell how much P&L the
    // horizontal distance represents, which is the whole point of the rotated overlay.
    // In a 200px strip every round value would overprint its neighbours, so outward from
    // $0 a tick is kept only when it clears the last kept one on its side.
    const ticks: OverlayShape['ticks'] = [];
    const MIN_TICK_GAP = 46;
    for (const side of [1, -1]) {
      let lastX = zeroX;
      const values = pnlTicks(geometry.maxAbsPnl)
        .filter(value => value * side > 0)
        .sort((a, b) => Math.abs(a) - Math.abs(b));
      for (const value of values) {
        const x = zeroX + value * scale;
        if (x < 18 || x > panelWidth - 18 || Math.abs(x - lastX) < MIN_TICK_GAP) continue;
        ticks.push({ x, label: `${value >= 0 ? '+' : '−'}$${Math.abs(value)}` });
        lastX = x;
      }
    }

    // Sign-only bands, projected here rather than during render (a ref read during
    // render is not a legal way to get them). Clipped to the price range on screen, so a
    // profitable region is shaded wherever it is visible.
    const priceRange = data.length > 0
      ? {
          min: data.reduce((lowest, bar) => Math.min(lowest, bar.low), Number.POSITIVE_INFINITY),
          max: data.reduce((highest, bar) => Math.max(highest, bar.high), Number.NEGATIVE_INFINITY),
        }
      : null;
    const bandRects: OverlayShape['bandRects'] = [];
    for (const band of zoneBands(payoff, priceRange)) {
      const top = series.priceToCoordinate(band.to);
      const bottom = series.priceToCoordinate(band.from);
      if (top == null || bottom == null) continue;
      bandRects.push({
        sign: band.sign,
        y: Math.min(Number(top), Number(bottom)),
        height: Math.abs(Number(bottom) - Number(top)),
      });
    }

    setOverlay({
      width: plotWidth, panelWidth, height: CHART_HEIGHT, zeroX,
      curve: toPath(defined), fills, bandRects, ticks,
    });
    // `data` is a dep because the bands are clipped to the bars on screen.
  }, [payoff, isOptionView, viewport, context, data]);

  // Escape closes, and the dialog takes focus when it opens so the keyboard is not left
  // behind on the row that was clicked. (Full Tab containment is not implemented; the
  // chart's own canvas is focusable, so a trap would need a real focus scope.)
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!trade) return;
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [trade, onClose]);
  if (!trade) return null;


  const headerRight = isOptionView && context
    ? `${context.legs.length} leg${context.legs.length === 1 ? '' : 's'} · ${context.underlying.symbol || 'underlying unavailable'}`
    : `${trade.direction} · ${dayOf(trade.entryDate)} → ${dayOf(trade.exitDate)}`;

  // SCOPE AND DENOMINATOR, explicitly. The header used to show the clicked LEG's percent and
  // reason beside a chart of the whole structure, so a structure reading +$370 could show
  // +5.30% with a single leg's exit reason. The structure's own dollars are the headline when
  // there is more than one leg, and the percentage is labelled for what it is a percentage of.
  const structurePnl = context ? context.legs.reduce((total, leg) => total + (leg.pnl ?? 0), 0) : null;
  const structureLegs = context?.legs.length ?? 0;
  const structureMissingPnl = context ? context.legs.filter(leg => leg.pnl == null).length : 0;
  const isStructureView = isOptionView && structureLegs > 1;
  const structureNetEntry = isStructureView && payoff && payoff.available
    ? payoffSummary(payoff).netEntryLabel
    : null;

  const emptyMessage = isOptionView
    ? 'No cached daily bars for this underlying in the window. The leg table and the payoff below are unaffected.'
    : 'No bars in range.';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={`${trade.symbol || 'Trade'} daily chart`}
        className={`bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full p-4 max-h-[90vh] overflow-y-auto outline-none ${isOptionView ? 'max-w-6xl' : 'max-w-4xl'}`}
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              {trade.symbol || '—'} · daily
            </div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {headerRight} ·{' '}
              {isStructureView && structurePnl != null ? (
                <>
                  <span className={structurePnl >= 0 ? 'text-green-600' : 'text-red-600'}>
                    structure {structurePnl >= 0 ? '+' : '−'}${Math.abs(structurePnl).toFixed(2)}
                  </span>
                  {structureNetEntry ? ` on ${structureNetEntry}` : ''}
                  {' · '}
                  <span title="The row you clicked is one leg of this structure">
                    clicked row: leg of {structureLegs}, {trade.pnlPercent >= 0 ? '+' : ''}
                    {trade.pnlPercent.toFixed(2)}%
                  </span>
                  {structureMissingPnl > 0
                    ? ` · ${structureMissingPnl} leg${structureMissingPnl === 1 ? '' : 's'} without a recorded P&L`
                    : ''}
                </>
              ) : (
                <>
                  <span className={trade.pnl >= 0 ? 'text-green-600' : 'text-red-600'}>
                    {trade.pnl >= 0 ? '+' : ''}{trade.pnlPercent.toFixed(2)}%
                  </span>
                  {' · '}{trade.exitReason}
                </>
              )}
            </div>
          </div>
          <button onClick={onClose}
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-2xl leading-none px-2">×</button>
        </div>

        {isOptionView && (
          <div className="flex flex-wrap items-center gap-3 mb-2 text-xs text-gray-600 dark:text-gray-300">
            <label className="inline-flex items-center gap-1">
              <input type="checkbox" checked={showStructureMarkers}
                     onChange={e => setShowStructureMarkers(e.target.checked)} />
              Structure markers
            </label>
            <label className="inline-flex items-center gap-1">
              <input type="checkbox" checked={showLegMarkers}
                     onChange={e => setShowLegMarkers(e.target.checked)} />
              Leg markers
            </label>
            <label className="inline-flex items-center gap-1">
              <input type="checkbox" checked={showOverlay}
                     onChange={e => setShowOverlay(e.target.checked)} />
              Expiry payoff
            </label>
            <span className="text-[11px] text-gray-500 dark:text-gray-400">
              Right panel: the structure's P&amp;L if held to expiration, at each underlying price
              (same price axis). Green/red zones mark where that is a profit or a loss. An early
              exit is priced off the option's premium (time value included), so it can profit
              in the red zone. Markers are labels on the bar, not price levels.
            </span>
          </div>
        )}

        {!trade.symbol
          ? <div className="flex items-center justify-center text-sm text-gray-500" style={{ height: CHART_HEIGHT }}>No symbol recorded on this trade (re-run a backtest to populate symbols).</div>
          : loading
            ? <div className="flex items-center justify-center text-sm text-gray-500" style={{ height: CHART_HEIGHT }}>Loading {isOptionView ? 'trade context' : 'daily bars'}…</div>
            : error
              ? <div className="flex items-center justify-center text-sm text-red-500" style={{ height: CHART_HEIGHT }}>Could not load: {error}</div>
              : !dataReady
                ? <div className="flex items-center justify-center text-sm text-gray-500 px-4 text-center" style={{ height: CHART_HEIGHT }}>{emptyMessage}</div>
                : (
                  <div className="flex items-stretch">
                  <div className="relative flex-1 min-w-0">
                    <div ref={containerRef} />
                    {isOptionView && overlay && (
                      <svg
                        className="absolute left-0 top-0 pointer-events-none"
                        width={overlay.width} height={overlay.height}
                        viewBox={`0 0 ${overlay.width} ${overlay.height}`}
                        aria-hidden="true">
                        {overlay.bandRects.map((band, index) => (
                          // A flat-zero interval is NEUTRAL: not shaded as profit.
                          band.sign === 'neutral' ? null : (
                            <rect key={`band-${index}`} x={0} y={band.y}
                                  width={overlay.width} height={band.height}
                                  fill={band.sign === 'profit' ? '#16a34a' : '#dc2626'}
                                  fillOpacity={0.06} />
                          )
                        ))}
                      </svg>
                    )}
                  </div>
                  {isOptionView && overlay && showOverlay && (
                    <div className="shrink-0 border-l border-gray-200 dark:border-gray-700"
                         style={{ width: overlay.panelWidth }}>
                      <svg width={overlay.panelWidth} height={overlay.height}
                           viewBox={`0 0 ${overlay.panelWidth} ${overlay.height}`}
                           role="img" aria-label="Profit or loss at expiration by underlying price">
                        {overlay.bandRects.map((band, index) => (
                          band.sign === 'neutral' ? null : (
                            <rect key={`pband-${index}`} x={0} y={band.y}
                                  width={overlay.panelWidth} height={band.height}
                                  fill={band.sign === 'profit' ? '#16a34a' : '#dc2626'}
                                  fillOpacity={0.06} />
                          )
                        ))}
                        {overlay.fills.map((fill, index) => (
                          <path key={`fill-${index}`} d={fill.path}
                                fill={fill.sign === 'profit' ? '#16a34a' : '#dc2626'}
                                fillOpacity={0.18} stroke="none" />
                        ))}
                        <line x1={overlay.zeroX} y1={30} x2={overlay.zeroX} y2={overlay.height - 20}
                              stroke="#94a3b8" strokeWidth={1} strokeDasharray="4 4" />
                        <path d={overlay.curve} fill="none" stroke="#22d3ee" strokeWidth={2} />
                        <text x={overlay.panelWidth / 2} y={11} textAnchor="middle" fontSize={10}
                              fontWeight={600} fill="#cbd5e1">P&amp;L at expiry</text>
                        {overlay.ticks.map(tick => (
                          <g key={`tick-${tick.label}`}>
                            <line x1={tick.x} y1={24} x2={tick.x} y2={29} stroke="#94a3b8" strokeWidth={1} />
                            <text x={tick.x} y={22} textAnchor="middle" fontSize={9} fill="#94a3b8">{tick.label}</text>
                          </g>
                        ))}
                        <text x={overlay.zeroX} y={22} textAnchor="middle" fontSize={9} fill="#cbd5e1">$0</text>
                        <text x={overlay.zeroX - 4} y={overlay.height - 8} textAnchor="end" fontSize={9} fill="#f87171">loss</text>
                        <text x={overlay.zeroX + 4} y={overlay.height - 8} textAnchor="start" fontSize={9} fill="#4ade80">profit</text>
                      </svg>
                    </div>
                  )}
                  </div>
                )}

        {isOptionView && context && (
          <div className="mt-3">
            <OptionTradeDetails context={context} scope={effectiveScope} onScopeChange={setScope} />
          </div>
        )}
      </div>
    </div>
  );
};

export default TradeChartModal;
