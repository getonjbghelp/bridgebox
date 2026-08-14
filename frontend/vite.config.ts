import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base: './' so the built assets load with relative paths - pywebview
// serves the production build straight off disk, not from a domain root.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    strictPort: true,
  },
})
