import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

// Portas configuráveis via env (não colidem com outras POCs). Defaults 8765/5290.
const API_PORT = process.env.API_PORT || '8765'
const WEB_PORT = process.env.WEB_PORT || '5290'

export default defineConfig({
  plugins: [react()],
  define: { global: 'globalThis' },
  resolve: {
    alias: {
      buffer: require.resolve('buffer/'),
      events: require.resolve('events/'),
      process: require.resolve('process/browser'),
      stream: require.resolve('stream-browserify'),
    },
  },
  server: {
    port: Number(WEB_PORT),
    strictPort: true,
    proxy: { '/api': `http://localhost:${API_PORT}` },
  },
  preview: {
    port: Number(WEB_PORT),
    strictPort: true,
    proxy: { '/api': `http://localhost:${API_PORT}` },
  },
})
