import { describe, it, expect } from 'vitest';
import { isFlagField, isVocabularyFlagField } from './ConditionBuilder';
import type { AvailableField } from './ConditionBuilder';

const vocabFlag: AvailableField = { field: 'has_position', fieldType: 'flag', description: 'In position', isBoolean: true };
const vocabNumeric: AvailableField = { field: 'pnl_pct', fieldType: 'numeric', description: 'PnL %', isBoolean: false };
const legacyBool: AvailableField = { field: 'position:in_position', fieldType: 'position', description: 'In position', isBoolean: true };
const entryNumeric: AvailableField = { field: 'model_probability', fieldType: 'model_probability', description: 'Prob' };

describe('isFlagField', () => {
  it('treats vocabulary flags as flags (no operator/value)', () => {
    expect(isFlagField(vocabFlag)).toBe(true);
  });
  it('treats legacy boolean entry fields as flags (back-compat)', () => {
    expect(isFlagField(legacyBool)).toBe(true);
  });
  it('treats numeric vocabulary fields as non-flags', () => {
    expect(isFlagField(vocabNumeric)).toBe(false);
  });
  it('treats entry numeric (no isBoolean) as non-flags', () => {
    expect(isFlagField(entryNumeric)).toBe(false);
  });
  it('treats undefined (no field selected) as non-flag', () => {
    expect(isFlagField(undefined)).toBe(false);
  });
});

describe('isVocabularyFlagField', () => {
  it('is true only for vocabulary flags (fieldType "flag")', () => {
    expect(isVocabularyFlagField(vocabFlag)).toBe(true);
  });
  it('is false for legacy boolean entry fields (so they keep their operator select)', () => {
    expect(isVocabularyFlagField(legacyBool)).toBe(false);
  });
  it('is false for numerics and undefined', () => {
    expect(isVocabularyFlagField(vocabNumeric)).toBe(false);
    expect(isVocabularyFlagField(undefined)).toBe(false);
  });
});
