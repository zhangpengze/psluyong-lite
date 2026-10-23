import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import pkg from './package.json'

// Tauri 需要一个固定端口，端口被占用时直接失败而不是自动换端口
export default defineConfig({
  plugins: [vue()],
  clearScreen: false,
  // 版本号编译期注入：浏览器预览里也能显示（Tauri 的 app.getVersion 拿不到时不影响）
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
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
