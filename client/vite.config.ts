import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [preact()],
  base: './',
  build: {
    outDir:   '../socialhome/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // Strip the `Origin` header in dev — the backend's cors-deny
      // middleware (`socialhome.hardening.build_cors_deny_middleware`)
      // refuses any request carrying an unallowed Origin, and Vite's
      // proxy forwards the original `Origin: http://localhost:5173`
      // by default. With Origin removed the request is indistinguishable
      // from a same-origin call, which is what the dev SPA effectively
      // is once Vite is in front of it. Production deploys serve the
      // SPA from the backend itself, so this only affects `pnpm run dev`.
      '/api': {
        target: 'http://localhost:8099',
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => proxyReq.removeHeader('origin'))
        },
      },
      '/ws':  { target: 'ws://localhost:8099', ws: true },
    },
  },
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
})
