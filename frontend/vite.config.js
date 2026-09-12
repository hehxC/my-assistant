import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 负责启动 Vue 开发服务器并构建生产资源。
export default defineConfig({
  // 启用 Vue 单文件组件支持。
  plugins: [vue()],
  server: {
    proxy: {
      // 开发环境下把 /api 请求转发给本地 FastAPI 服务。
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
