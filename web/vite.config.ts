import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// base './' keeps asset URLs relative so the bundle works at any GitHub Pages
// path (user site, repo subpath, or custom domain) without per-repo config.
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
})
