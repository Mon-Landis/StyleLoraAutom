import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// 开发期：浏览器访问 http://127.0.0.1:5173，/api 代理到后端。
// 后端须以 `stylelora serve --port 8000 --dev-origin http://127.0.0.1:5173` 启动：
// Host 由 changeOrigin 改写为 127.0.0.1:8000，Origin 原样透传并与 dev_origin 比对。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    globals: false,
  },
});
