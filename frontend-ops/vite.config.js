import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5178,
    strictPort: true,
    host: "127.0.0.1",
    proxy: {
      '/api': {
        target: process.env.FUSION_API_TARGET || 'http://127.0.0.1:8010',
        changeOrigin: true
      },
      '/static': {
        target: process.env.FUSION_API_TARGET || 'http://127.0.0.1:8010',
        changeOrigin: true
      }
    }
  }
})
