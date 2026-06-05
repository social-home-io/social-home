/// <reference types="vite/client" />

// Side-effect CSS imports (``import './app.css'``). TypeScript 6 errors on
// a side-effect import with no module declaration; the bundler (Vite) owns
// the actual resolution, so a bare ambient module is all tsc needs.
declare module '*.css'
