import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { HashRouter } from 'react-router-dom';
import './styles/tokens.css';
import { App } from './App';
import { useApp } from './state/app';

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false, staleTime: 5000 } },
});

// 主题/密度只通过根节点属性切换，不做条件渲染。
useApp.subscribe((s) => {
  document.documentElement.dataset.theme = s.theme;
  document.documentElement.dataset.density = s.density;
});
document.documentElement.dataset.theme = useApp.getState().theme;
document.documentElement.dataset.density = useApp.getState().density;

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <HashRouter>
        <App />
      </HashRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
