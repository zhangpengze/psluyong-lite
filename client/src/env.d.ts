/// <reference types="vite/client" />

/** vite.config.ts 编译期注入的客户端版本号（来自 client/package.json） */
declare const __APP_VERSION__: string

// 没有这个声明，`tsc --noEmit` 连 main.ts 的 `import App from './App.vue'`
// 都过不了（报 TS2307），整个项目等于没有类型检查。
declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
