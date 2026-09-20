// Offline browser review harness. Network requests are intercepted by browser.cjs.
import React from '../../testplatform/frontend/node_modules/react';
import { createRoot } from '../../testplatform/frontend/node_modules/react-dom/client';
import TradeChartModal from '../../testplatform/frontend/src/components/TradeChartModal';
import { allMarkers } from '../../testplatform/frontend/src/lib/optionChartView';

const data = (window as any).reviewContext;
(window as any).reviewMarkers = allMarkers(data.legs);
const selected = {backtestId: 77, tradeId: 1};
createRoot(document.getElementById('root')!).render(
  <TradeChartModal trade={{symbol: 'XYZ', entryDate: '2026-09-08', exitDate: '2026-09-11',
    direction: 'long', entryPrice: 8, exitPrice: 13.3, pnl: 530, pnlPercent: 5.3, exitReason: 'exit'}}
    optionSelection={selected} onClose={() => {}} />,
);
