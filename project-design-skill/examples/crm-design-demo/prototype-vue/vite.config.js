import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: './',                 // 相对资源路径 → dist 可静态打开(file:// 或任意静态服务器)
  plugins: [vue()],
  server: { host: true, port: 5173 }
})
