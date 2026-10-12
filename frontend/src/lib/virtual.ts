// 虚拟滚动窗口计算（纯函数）。网格按行虚拟化，单元格定高。
export interface GridMetrics {
  width: number;
  height: number;
  scrollTop: number;
  cellMin: number;
  cellHeight: number;
  gap: number;
  count: number;
  overscanRows?: number;
}
export interface GridWindow {
  cols: number;
  rowHeight: number;
  totalRows: number;
  totalHeight: number;
  firstRow: number;
  lastRow: number; // 不含
  firstIndex: number;
  lastIndex: number; // 不含
}

export function columnsFor(width: number, cellMin: number, gap: number): number {
  return Math.max(1, Math.floor((width + gap) / (cellMin + gap)));
}

export function computeWindow(m: GridMetrics): GridWindow {
  const cols = columnsFor(m.width, m.cellMin, m.gap);
  const rowHeight = m.cellHeight + m.gap;
  const totalRows = Math.ceil(m.count / cols);
  const over = m.overscanRows ?? 2;
  const first = Math.max(0, Math.floor(m.scrollTop / rowHeight) - over);
  const last = Math.min(totalRows, Math.ceil((m.scrollTop + m.height) / rowHeight) + over);
  return {
    cols,
    rowHeight,
    totalRows,
    totalHeight: Math.max(0, totalRows * rowHeight - m.gap),
    firstRow: first,
    lastRow: Math.max(first, last),
    firstIndex: Math.min(m.count, first * cols),
    lastIndex: Math.min(m.count, Math.max(first, last) * cols),
  };
}

/** 让 index 所在行完整可见所需的 scrollTop；已可见则原样返回。 */
export function scrollTopFor(index: number, cols: number, rowHeight: number, viewH: number, cur: number): number {
  const row = Math.floor(index / cols);
  const top = row * rowHeight;
  const bottom = top + rowHeight;
  if (top < cur) return top;
  if (bottom > cur + viewH) return bottom - viewH;
  return cur;
}
