/**
 * Reading a stored rule ACTION's value and reference.
 *
 * A stored action carries each field twice — `action_value` / `actionValue`,
 * `reference_value` / `referenceValue` — and for the value the two copies are NOT
 * interchangeable. On an optimization run the GA writes its tuned number into the SNAKE key
 * (its genes are literally named `exit:<rule>:a0:action_value`) and leaves the camel copy at
 * whatever the strategy template was seeded with. Backtest 1331, for example, stores:
 *
 *     { action_type: "adjust_take_profit", actionValue: -5.0, action_value: -10.0, value: -10.0 }
 *
 * -5 is the template default; -10 is what actually ran. Every FMPRating run sampled carried the
 * same stale -5 in the camel key, so a reader that preferred camel reported the untuned default
 * for every optimized run — while the rule list beside it, reading snake, showed the truth.
 *
 * Snake therefore wins. Camel is a fallback only, for hand-built legacy rows that never had a
 * snake copy at all.
 */
export const EXIT_REF_LABEL: Record<string, string> = {
  order_open_price: 'entry',
  expert_target_price: 'analyst target',
  current_price: 'price',
};

export function actionValueOf(rule: any): number | undefined {
  const v = rule?.action_value ?? rule?.value ?? rule?.actionValue;
  return typeof v === 'number' ? v : undefined;
}

export function actionRefOf(rule: any): string {
  const ref = rule?.reference_value ?? rule?.referenceValue;
  return ref ? (EXIT_REF_LABEL[ref] ?? ref) : '';
}
