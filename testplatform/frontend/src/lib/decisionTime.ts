// The default entry/decision time (exchange-local HH:MM) of a NEW stock backtest schedule.
//
// MUST equal ba2_common.core.knowability.DEFAULT_DECISION_TIME (the backend, launcher, robustness
// suite and deploy tools read that one); backend/tests/test_decision_time_single_source.py fails
// when the two drift. One bar or more after the session's first bar: the decision price is the
// close of the latest 5-minute bar that has ENDED, so a decision on the first bar (09:30) would
// see only the previous close.
export const DEFAULT_DECISION_TIME = '09:40';
