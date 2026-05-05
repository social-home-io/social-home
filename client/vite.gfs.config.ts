/* Separate Vite build for the GFS-served public surface.
 *
 * Today this only emits the public-story viewer bundle (read by the
 * Preact entry in `gfs/public_story.tsx`). Future GFS-side surfaces
 * (admin UI port, public global-space pages) plug in here as
 * additional rollup inputs so they share the build infra without
 * dragging the SPA bundle's size + chunk strategy along.
 *
 * Output lands directly in the Python package at
 * `socialhome/global_server/static/` so `socialhome-global-server`
 * ships the bundles without a separate dist step.
 */
import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [preact()],
  build: {
    outDir: '../socialhome/global_server/static',
    emptyOutDir: false, // keep alongside admin / future bundles
    lib: {
      entry: resolve(__dirname, 'gfs/public_story.tsx'),
      name: 'PublicStoryViewer',
      formats: ['iife'],
      fileName: () => 'story_public_viewer.js',
    },
    rollupOptions: {
      output: {
        // Single self-contained file so the GFS landing page can
        // ship one ``<script src=…>`` tag with no chunked imports.
        inlineDynamicImports: true,
      },
    },
  },
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
})
