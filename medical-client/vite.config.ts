import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 患者前台，端口 3001，避免与后台管理（3000）冲突
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3001,
    proxy: {
      '/api': {
        target: process.env.BACKEND_PROXY_TARGET || 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
})
