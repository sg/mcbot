import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Build output goes straight into webapi/static so FastAPI serves it.
// Dev server proxies /api (and /api/ws/*) to the running bot on :8080.
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: fileURLToPath(new URL('../webapi/static', import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
