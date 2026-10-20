import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Tauri 需要一个固定端口，端口被占用时直接失败而不是自动换端口
export default defineConfig({
  plugins: [vue()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    host: '127.0.0.1',
  },
  build: {
    target: 'chrome105',
    outDir: 'dist',
    emptyOutDir: true,
  },
})
