import { describe, expect, it } from 'vitest';
import { columnsFor, computeWindow, scrollTopFor } from '../src/lib/virtual';

const base = { width: 500, height: 300, scrollTop: 0, cellMin: 100, cellHeight: 100, gap: 0, count: 1000, overscanRows: 1 };

describe('virtual window', () => {
  it('computes columns with gap', () => {
    expect(columnsFor(500, 100, 0)).toBe(5);
    expect(columnsFor(500, 100, 10)).toBe(4);
    expect(columnsFor(10, 100, 0)).toBe(1);
  });

  it('renders only visible rows plus overscan', () => {
    const w = computeWindow(base);
    expect(w.cols).toBe(5);
    expect(w.totalRows).toBe(200);
    expect(w.firstRow).toBe(0);
    expect(w.lastRow).toBe(4); // 3 visible + 1 overscan
    expect(w.lastIndex).toBe(20);
  });

  it('shifts window with scrollTop and clamps at the end', () => {
    const mid = computeWindow({ ...base, scrollTop: 1000 });
    expect(mid.firstRow).toBe(9);
    expect(mid.firstIndex).toBe(45);
    const end = computeWindow({ ...base, scrollTop: 1e9 });
    expect(end.lastIndex).toBe(1000);
  });

  it('handles empty lists', () => {
    const w = computeWindow({ ...base, count: 0 });
    expect(w.totalHeight).toBe(0);
    expect(w.lastIndex).toBe(0);
  });

  it('scrollTopFor keeps index visible', () => {
    expect(scrollTopFor(0, 5, 100, 300, 500)).toBe(0);
    expect(scrollTopFor(50, 5, 100, 300, 0)).toBe(800);
    expect(scrollTopFor(5, 5, 100, 300, 0)).toBe(0);
  });
});
