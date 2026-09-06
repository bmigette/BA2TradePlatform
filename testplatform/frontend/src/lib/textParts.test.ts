import { describe, expect, it } from 'vitest';
import { boldNumbers } from './textParts';

const render = (s: string) => boldNumbers(s).map(p => (p.bold ? `[${p.text}]` : p.text)).join('');

describe('boldNumbers', () => {
  it('marks the threshold in a condition', () => {
    expect(render('confidence >= 35')).toBe('confidence >= [35]');
    expect(render('days_opened > 60')).toBe('days_opened > [60]');
  });

  it('marks a signed percentage in an action, sign included', () => {
    // "+6%" is one token: bolding only the 6 would orphan the sign that gives it meaning.
    expect(render('Move take profit → analyst target +6%')).toBe('Move take profit → analyst target [+6%]');
    expect(render('Move stop loss → entry -10%')).toBe('Move stop loss → entry [-10%]');
  });

  it('marks decimals whole', () => {
    expect(render('expected_profit >= 2.5')).toBe('expected_profit >= [2.5]');
  });

  it('leaves a flag condition untouched', () => {
    expect(render('has_no_position')).toBe('has_no_position');
    expect(boldNumbers('bullish')).toEqual([{ text: 'bullish', bold: false }]);
  });

  it('does NOT bold digits that continue an identifier', () => {
    // `atr_14` and `sma200` are field names; half-bolding one reads as a rendering bug.
    expect(render('atr_14 > 2')).toBe('atr_14 > [2]');
    expect(render('sma200 crossed')).toBe('sma200 crossed');
  });

  it('handles several numbers in one line', () => {
    expect(render('(confidence >= 35 AND expected_profit >= 3)'))
      .toBe('(confidence >= [35] AND expected_profit >= [3])');
  });

  it('returns nothing for empty input', () => {
    expect(boldNumbers('')).toEqual([]);
  });

  it('round-trips the original text exactly', () => {
    const s = 'Move take profit → analyst target +6% after 2 days';
    expect(boldNumbers(s).map(p => p.text).join('')).toBe(s);
  });
});
