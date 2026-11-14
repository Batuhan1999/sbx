import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

// Built into sbx_control/web_dist, served by sbx_control/spa.py under a
// self-only CSP: no inlined assets, no module-preload polyfill (both would
// put a <script> or data: URL into the page).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  base: '/',
  build: {
    outDir: '../control/sbx_control/web_dist',
    emptyOutDir: true,
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/auth': 'http://127.0.0.1:8080',
      '/cli': 'http://127.0.0.1:8080',
      '/connections': 'http://127.0.0.1:8080',
    },
  },
})
