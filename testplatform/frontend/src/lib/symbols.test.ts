import { describe, it, expect } from 'vitest';
import { parseSymbols } from './symbols';

describe('parseSymbols', () => {
  it('uppercases, dedups, splits on commas/space/newlines', () => {
    expect(parseSymbols('aapl, msft\nNVDA aapl')).toEqual(['AAPL', 'MSFT', 'NVDA']);
  });
  it('drops empties', () => {
    expect(parseSymbols('  ,, \n ')).toEqual([]);
  });
  it('splits on semicolons too', () => {
    expect(parseSymbols('aapl;msft ; nvda')).toEqual(['AAPL', 'MSFT', 'NVDA']);
  });
});
