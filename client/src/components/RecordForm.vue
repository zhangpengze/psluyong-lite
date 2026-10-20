<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, type ClientConfig, type ExportResult, type TemplateItem } from '../api/sidecar'

const props = defineProps<{
  config: ClientConfig | null
  templates: TemplateItem[]
  partners: string[]
  ready: boolean
}>()

const emit = defineEmits<{ exported: []; 'need-settings': [] }>()

const templateId = ref('')
const partner = ref('')
const author = ref('')
const title = ref('')
const busy = ref(false)
const result = ref<ExportResult | null>(null)
const elapsed = ref(0)
let timer: number | undefined

const canSubmit = computed(
  () =>
    !busy.value &&
    props.ready &&
    !!templateId.value &&
    author.value.trim().length > 0 &&
    title.value.trim().length > 0,
)

// 记住上次用的模板与合作伙伴，第二次生成不用重新选
watch(
  () => props.config,
  (cfg) => {
    if (!cfg) return
    if (!templateId.value && cfg.last_template_id) templateId.value = cfg.last_template_id
    if (!partner.value && cfg.last_partner) partner.value = cfg.last_partner
  },
  { immediate: true },
)

watch(
  () => props.templates,
  (list) => {
    if (templateId.value && list.some((t) => t.id === templateId.value)) return
    if (!templateId.value && list.length === 1) templateId.value = list[0].id
  },
)

async function submit() {
  if (!canSubmit.value) return

  busy.value = true
  result.value = null
  elapsed.value = 0
  // 生成通常 5-20 秒，给个跳动的秒数，员工不会以为卡死
  timer = window.setInterval(() => (elapsed.value += 1), 1000)

  try {
    const start = await api.exportImage({
      template_id: templateId.value,
      author: author.value.trim(),
      title: title.value.trim(),
      partner_name: (partner.value || '').trim(),
    })
    if (!start.ok) {
      result.value = { ok: false, error: start.error }
      return
    }

    // 轮询 worker 子进程：setTimeout 链式调度，上一次没回来绝不叠加请求
    const finalRes = await poll(start.task_id)
    result.value = finalRes

    if (finalRes.ok) {
      emit('exported')
      // 记住这次的组合
      await api.saveConfig({ last_template_id: templateId.value, last_partner: partner.value })
      if (props.config?.auto_clear) {
        author.value = ''
        title.value = ''
      }
    }
  } catch (e) {
    result.value = { ok: false, error: e instanceof Error ? e.message : '生成失败' }
  } finally {
    if (timer) window.clearInterval(timer)
    busy.value = false
  }
}

function poll(taskId: string): Promise<ExportResult> {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const s = await api.exportStatus(taskId)
        if (s.finished) {
          resolve(s)
        } else {
          window.setTimeout(tick, 1500)
        }
      } catch (e) {
        reject(e)
      }
    }
    tick()
  })
}

function openFolder() {
  const path = result.value?.ok ? result.value.image_path : props.config?.output_dir || ''
  if (path) api.openFolder(path)
}

function retry() {
  result.value = null
}
</script>

<template>
  <div>
    <div class="card">
      <div class="field">
        <label>模板<span class="req">*</span></label>
        <select v-model="templateId" :disabled="busy || templates.length === 0">
          <option value="" disabled>
            {{ templates.length ? '请选择模板' : '暂无模板，请到设置里上传 PSD' }}
          </option>
          <option v-for="t in templates" :key="t.id" :value="t.id">
            {{ t.name }}
          </option>
        </select>
        <div v-if="props.config" class="hint">
          模板里的「{{ props.config.author_placeholder }}」会换成作者、「{{ props.config.title_placeholder }}」会换成标题（可在设置里改）
        </div>
      </div>

      <div class="field">
        <label>合作伙伴</label>
        <input v-model="partner" type="text" list="partner-list" placeholder="选择或直接输入" :disabled="busy" />
        <datalist id="partner-list">
          <option v-for="p in partners" :key="p" :value="p" />
        </datalist>
      </div>

      <div class="field">
        <label>作者<span class="req">*</span></label>
        <input v-model="author" type="text" placeholder="例如：张三" :disabled="busy" />
      </div>

      <div class="field">
        <label>标题<span class="req">*</span></label>
        <input v-model="title" type="text" placeholder="例如：基于深度学习的图像分割方法研究" :disabled="busy" />
      </div>

      <button class="btn" :disabled="!canSubmit" @click="submit">
        {{ busy ? `正在生成… ${elapsed}s` : '生成录用图' }}
      </button>
      <div v-if="!ready" class="hint" style="margin-top: 8px">
        还没配置完整，<a style="color: #2563eb; cursor: pointer" @click="emit('need-settings')">去设置</a>
      </div>
    </div>

    <div v-if="result" class="result" :class="result.ok ? 'ok' : 'fail'">
      <template v-if="result.ok">
        <div class="head"><span class="ico">✓</span>生成成功（{{ (result.duration_ms / 1000).toFixed(1) }}s）</div>
        <div class="t2" style="font-size: 12.5px">{{ result.image_name }}</div>

        <div class="preview">
          <img :src="api.previewUrl(result.image_path)" alt="成品预览" />
        </div>

        <div style="margin-top: 9px; font-size: 12.5px">
          <span v-if="result.uploaded" style="color: #059669">已同步到服务器</span>
          <span v-else-if="result.queued" style="color: #d97706">
            已保存在本地，联网后自动补传{{ result.upload_message ? `（${result.upload_message}）` : '' }}
          </span>
          <span v-else style="color: #d97706">{{ result.upload_message || '未同步' }}</span>
        </div>

        <div v-if="result.warnings.length" style="margin-top: 8px">
          <div style="font-weight: 700; color: #92400e; font-size: 12.5px">需要注意：</div>
          <ul style="color: #92400e">
            <li v-for="w in result.warnings" :key="w">{{ w }}</li>
          </ul>
        </div>

        <button class="btn ghost" style="margin-top: 10px" @click="openFolder">打开文件夹</button>
      </template>

      <template v-else>
        <div class="head"><span class="ico">!</span>生成失败</div>
        <div>{{ result.error }}</div>
        <button class="btn ghost" style="margin-top: 10px" @click="retry">重新填写</button>
      </template>
    </div>
  </div>
</template>
