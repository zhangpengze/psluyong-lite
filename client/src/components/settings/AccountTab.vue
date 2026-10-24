<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ClientConfig } from '../../api/sidecar'
import { api } from '../../api/sidecar'
import { useToast } from '../../composables/useToast'
import { isHttpUrl } from '../../utils/validate'

/** 设置 · 账号分组：姓名 / 服务器地址（含测试连接）/ API Key */
const props = defineProps<{
  form: ClientConfig
  /** 外壳 /status 轮询带下来的服务器连通状态 */
  serverOk: boolean
}>()

const { toast } = useToast()
const showKey = ref(false)
const testing = ref(false)

const urlOk = computed(() => isHttpUrl(props.form.server_url))

/**
 * 测试连接：用**表单里此刻填的**地址和 Key 真发一次请求。
 *
 * 以前的实现打 /status，而 /status 读的是磁盘上的旧配置 —— 员工改了地址点测试，
 * 提示里打印的是新地址，实际测的是老地址：说"连接成功"，一保存就连不上。
 * 现在四档结论分开报，"地址不通"和"Key 不对"是两句完全不同的话。
 */
async function testConnection() {
  if (!urlOk.value) {
    toast('服务器地址应以 http:// 开头', 'error')
    return
  }
  testing.value = true
  try {
    const { result } = await api.testConfig(props.form.server_url, props.form.api_key)
    const type = result.level === 'ok' ? 'success' : result.level === 'server_error' ? 'warning' : 'error'
    toast(result.message, type)
  } catch (e) {
    toast(e instanceof Error ? e.message : '连接失败', 'error')
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <div class="field">
    <label>你的姓名<span class="req">*</span></label>
    <input v-model="form.employee_name" type="text" placeholder="例如：张三" />
    <div class="hint">主管后台按这个姓名统计工作量，请与公司内部称呼一致</div>
  </div>
  <div class="field">
    <label>服务器地址<span class="req">*</span></label>
    <div class="row" style="align-items: center">
      <input v-model="form.server_url" type="text" placeholder="http://192.168.1.10:8080" style="flex: 1 1 auto; min-width: 0" />
      <button class="btn ghost sm" style="flex: 0 0 auto; width: auto; white-space: nowrap" :disabled="testing" @click="testConnection">
        {{ testing ? '测试中…' : '测试连接' }}
      </button>
    </div>
    <div class="hint">
      当前状态：<span :style="{ color: serverOk ? 'var(--ok)' : 'var(--err)', fontWeight: 600 }">{{ serverOk ? '可连接' : '连不上' }}</span>
    </div>
  </div>
  <div class="field">
    <label>API Key<span class="req">*</span></label>
    <div class="secret-wrap">
      <input v-model="form.api_key" :type="showKey ? 'text' : 'password'" placeholder="向主管索取" />
      <button class="sec-btn" :title="showKey ? '隐藏密钥' : '显示密钥'" :aria-label="showKey ? '隐藏密钥' : '显示密钥'" @click="showKey = !showKey">
        <svg v-if="!showKey" viewBox="0 0 16 16" width="14" height="14">
          <path d="M1.5 8S3.8 3.8 8 3.8 14.5 8 14.5 8 12.2 12.2 8 12.2 1.5 8 1.5 8z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
          <circle cx="8" cy="8" r="1.8" fill="none" stroke="currentColor" stroke-width="1.3" />
        </svg>
        <svg v-else viewBox="0 0 16 16" width="14" height="14">
          <path d="M2.5 2.5l11 11" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
          <path d="M1.5 8S3.8 3.8 8 3.8c1.2 0 2.2.3 3.1.8M14.5 8s-.8 1.4-2.1 2.6M6 6.2A3.4 3.4 0 0 0 8 11.8a3.3 3.3 0 0 0 1.9-.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
        </svg>
      </button>
    </div>
    <div class="hint">每个员工一个，用来向服务器证明身份；丢了找主管重新要</div>
  </div>
</template>
