import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  // Always embed under Next K Protocol /clawby-ui/. Override with CLAWBY_UI_BASE=/ for standalone.
  base: process.env.CLAWBY_UI_BASE || '/clawby-ui/',
  plugins: [react()],
  server: {
    proxy: { '/api': 'http://127.0.0.1:8899' },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
})
