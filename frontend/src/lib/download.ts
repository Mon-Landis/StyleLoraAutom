/** 文件名净化：去掉路径分隔符与控制字符，限长；不带路径。 */
export function safeFileName(name: string, ext: string): string {
  // eslint-disable-next-line no-control-regex
  const base = name.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').trim().slice(0, 64) || 'prompt-group';
  return `${base}.${ext}`;
}

/** 触发浏览器下载一个 Blob（json / zip 通用）。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function downloadJson(data: unknown, filename: string): void {
  downloadBlob(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }), filename);
}