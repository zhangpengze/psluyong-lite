<script setup lang="ts">
import type { ClientConfig } from '../../api/sidecar'

/** 设置 · 窗口分组：主窗置顶 / 自动贴边 / 关闭行为（主题不在这儿：主窗头部有切换按钮） */
defineProps<{
  form: ClientConfig
}>()
</script>

<template>
  <div class="field">
    <label>主窗置顶</label>
    <label class="switch">
      <input v-model="form.always_on_top" type="checkbox" />
      <span class="sw" />
      <span>{{ form.always_on_top ? '开' : '关' }}</span>
    </label>
    <div class="hint">开：主窗浮在其他应用之上，边看资料边录入；打开记录/设置窗时主窗自动让出</div>
  </div>
  <div class="field">
    <label>拖到边缘自动贴边</label>
    <label class="switch">
      <input v-model="form.auto_dock" type="checkbox" />
      <span class="sw" />
      <span>{{ form.auto_dock ? '开' : '关' }}</span>
    </label>
    <div class="hint">开：把主窗拖到任一屏缘 24px 内松手即贴边，<b>贴哪边看离哪边近</b>，不用选；关：只点主窗上的贴边按钮生效</div>
  </div>
  <div class="field">
    <label>关闭主窗时</label>
    <label class="switch">
      <input v-model="form.close_to_tray" type="checkbox" />
      <span class="sw" />
      <span>{{ form.close_to_tray ? '隐藏到托盘' : '直接退出' }}</span>
    </label>
    <div class="hint">隐藏到托盘：点关闭只是藏起来，从托盘菜单退出，防误关；直接退出：关闭即结束程序</div>
  </div>
</template>
