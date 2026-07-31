import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
   server: {
    host: '0.0.0.0',
    port: 3000,
    allowedHosts: ['papayaoyster.com', 'www.papayaoyster.com', '100.107.179.44',
                   'localhost', '127.0.0.1'],
    proxy: {
      '/api': {
        // Which backend `npm run dev` talks to. Defaults to the shared LAN
        // instance; set API_TARGET=http://127.0.0.1:8001 to hit a local dev
        // backend (see scripts/dev_backend.sh) without editing this file.
        target: process.env.API_TARGET || 'http://192.168.1.155:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      }
    }
  },
  build: {
    chunkSizeWarningLimit: 700,   // three.js base alone is ~555kB; allow headroom
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return
          if (id.includes('react') || id.includes('react-dom') || id.includes('react-router'))
            return 'vendor-react'
          if (id.includes('framer-motion'))
            return 'vendor-motion'
          if (id.includes('@tanstack'))
            return 'vendor-query'
          if (id.includes('lucide') || id.includes('react-markdown') || id.includes('remark'))
            return 'vendor-ui'
          if (id.includes('@supabase'))
            return 'vendor-supabase'
          if (id.includes('axios'))
            return 'vendor-axios'
          if (id.includes('three') || id.includes('postprocessing'))
            return 'vendor-three'
          return 'vendor-misc'
        },
      },
    },
  },
})
