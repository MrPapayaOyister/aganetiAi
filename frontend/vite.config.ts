import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://192.168.1.155:8000',
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