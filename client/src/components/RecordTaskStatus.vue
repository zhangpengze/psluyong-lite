<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, type ClientConfig, type ExportResult } from '../api/sidecar'
import { useToast } from '../composables/useToast'
import { copyText } from '../utils/clipboard'

/**
 * 录用页「生成状态 → 结果」条：
 * 生成中是一行提醒（心跳过期 / 可取消），结束后压成一行结果条，
 * 成功且带 PS 警告时折叠成一行计数，点开才看详情。
 */
const props = defineProps<{
  busy: boolean
  heartbeatStale: boolean
  cancelVisible: boolean
  result: ExportResult | null
  /** 打开文件夹的兜底目录（结果条失败态也会用到输出目录） */
  config: ClientConfig | null
}>()
const emit = defineEmits<{ retry: [] }>()

const { toast } = useToast()
// 结果条里的注意事项默认收起，点展开；换一次结果就收回
const warnOpen = ref(false)
// 结果条元素：成功后由 RecordForm 调 reveal() 把它滚进可视区
const rbarEl = ref<HTMLElement | null>(null)
function reveal() {
  rbarEl.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}
defineExpose({ reveal })
watch(
  () => props.result,
  () => {
    warnOpen.value = false
  },
)

function openFolder() {
  const path = props.result?.ok ? props.result.image_path : props.config?.output_dir || ''
  if (path) api.openFolder(path)
}

/** 复制成品路径：剪贴板不可用时由 copyText 内部退回调 + 选中 */
async function copyPath() {
  if (!props.result?.ok) return
  const ok = await copyText(props.result.image_path)
  toast(ok ? '路径已复制' : '复制失败，请手动复制', ok ? 'success' : 'error')
}
</script>

<template>
  <!-- 生成中只留一行关键警告；心跳过期时追加一句。
       配置缺项不在这里重复：那句话就在主按钮上（按钮自己变成「去配置：…」），
       同一个窗口里两处说一件事只会让 360px 更挤。 -->
  <div v-if="busy" class="busy-line">
    生成中请不要关闭软件或操作 Photoshop<span v-if="heartbeatStale">，耗时比平时长，请确认 PS 未卡在对话框</span>
    <span v-if="cancelVisible"> · 等太久可以点下方按钮取消</span>
  </div>

  <!-- 结果条：成功/失败都压成一行，注意事项折叠 -->
  <div v-if="result" ref="rbarEl" class="rbar" :class="result.ok ? 'ok' : 'fail'">
    <template v-if="result.ok">
      <span class="rb-ico">
        <svg viewBox="0 0 16 16" width="9" height="9">
          <path d="M5 8.2l2.1 2.1 4-4.2" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </span>
      <span class="rb-name" :title="`${result.image_name}（点击复制路径）`" @click="copyPath">{{ result.image_name }}</span>
      <span class="rb-meta">{{ (result.duration_ms / 1000).toFixed(1) }}s</span>
      <span class="rb-sync" :class="result.uploaded ? 'ok' : 'warn'">
        {{ result.uploaded ? '已同步' : '已存本地' }}
      </span>
      <button class="rb-btn" title="打开文件夹" aria-label="打开文件夹" @click="openFolder">
        <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round">
          <path d="M1.8 4.2a1 1 0 011-1h3.4l1.6 1.8h5.4a1 1 0 011 1v6a1 1 0 01-1 1h-10a1 1 0 01-1-1v-7.8z" />
        </svg>
      </button>
    </template>
    <template v-else>
      <span class="rb-ico">!</span>
      <span class="rb-msg" :title="result.error">{{ result.error }}</span>
      <button class="rb-btn" title="重新填写" aria-label="重新填写" @click="emit('retry')">
        <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M2.6 8a5.4 5.4 0 019.2-3.9M13.4 8a5.4 5.4 0 01-9.2 3.9" />
          <path d="M11.8 1.6v2.6H9.2M4.2 14.4v-2.6h2.6" />
        </svg>
      </button>
    </template>
  </div>

  <!-- 成功但带 PS 警告：折叠成一行计数，点开才看详情。
       警告 / 展开方向均为统一内联 SVG 描边图标（20px，与全项目同一套），不用字符符号。 -->
  <div v-if="result?.ok && result.warnings.length" class="rbar-warn">
    <button class="rw-toggle" @click="warnOpen = !warnOpen">
      <svg viewBox="0 0 16 16" width="20" height="20" aria-hidden="true">
        <path d="M8 1.6l6.4 11.2H1.6z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
        <path d="M8 6v3.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        <circle cx="8" cy="11.4" r="0.85" fill="currentColor" />
      </svg>
      {{ result.warnings.length }} 条注意事项
      <svg v-if="warnOpen" viewBox="0 0 16 16" width="20" height="20" aria-hidden="true">
        <path d="M4 10l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <svg v-else viewBox="0 0 16 16" width="20" height="20" aria-hidden="true">
        <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>
    <ul v-if="warnOpen">
      <li v-for="w in result.warnings" :key="w">{{ w }}</li>
    </ul>
  </div>
</template>
