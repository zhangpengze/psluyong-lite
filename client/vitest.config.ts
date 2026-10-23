import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  define: {
    // 组件里用到 vite.config.ts 注入的常量，测试环境给个占位值
    __APP_VERSION__: JSON.stringify('0.0.0-test'),
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.spec.ts'],
    // 沙箱/受限环境下多 worker 并行写临时缓存会 EPERM，串行反而更稳且这几秒够用
    fileParallelism: false,
  },
})
