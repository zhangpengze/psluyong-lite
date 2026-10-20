<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { api, type ClientConfig, type TemplateItem } from '../api/sidecar'

const props = defineProps<{
  config: ClientConfig | null
  serverOk: boolean
  templates: TemplateItem[]
}>()
const emit = defineEmits<{ saved: [ClientConfig]; 'templates-changed': [] }>()

const form = reactive<ClientConfig>({
  server_url: '',
  api_key: '',
  employee_name: '',
  ps_path: '',
  output_dir: '',
  jpg_quality: 10,
  auto_clear: true,
  last_template_id: '',
  last_partner: '',
  partners: [],
  template_dir: '',
  author_placeholder: '{{作者}}',
  title_placeholder: '{{标题}}',
  always_on_top: false,
  dock_side: '',
  win_x: -1,
  win_y: -1,
})

/** 名单用「一行一个」的文本框维护，比一堆输入框更适合批量粘贴 */
const partnersText = ref('')

const saving = ref(false)
const uploading = ref(false)
const message = ref('')
const messageType = ref<'ok' | 'fail'>('ok')
const fileInput = ref<HTMLInputElement | null>(null)

watch(
  () => props.config,
  (cfg) => {
    if (!cfg) return
    Object.assign(form, cfg)
    partnersText.value = (cfg.partners ?? []).join('\n')
  },
  { immediate: true },
)

const templateCount = computed(() => props.templates.length)

function flash(text: string, type: 'ok' | 'fail' = 'ok') {
  message.value = text
  messageType.value = type
  window.setTimeout(() => (message.value = ''), 4000)
}

async function save() {
  if (!form.employee_name.trim()) {
    flash('请填写你的姓名，主管后台按姓名统计', 'fail')
    return
  }
  if (!form.server_url.trim().startsWith('http')) {
    flash('服务器地址应以 http:// 开头', 'fail')
    return
  }

  saving.value = true
  try {
    const res = await api.saveConfig({
      server_url: form.server_url.trim().replace(/\/+$/, ''),
      api_key: form.api_key.trim(),
      employee_name: form.employee_name.trim(),
      ps_path: form.ps_path.trim(),
      output_dir: form.output_dir.trim(),
      jpg_quality: Number(form.jpg_quality),
      auto_clear: form.auto_clear,
      partners: partnersText.value.split('\n'),
      template_dir: form.template_dir.trim(),
      author_placeholder: form.author_placeholder.trim() || '{{作者}}',
      title_placeholder: form.title_placeholder.trim() || '{{标题}}',
    })
    emit('saved', res.config)
    flash('已保存')
  } catch (e) {
    flash(e instanceof Error ? e.message : '保存失败', 'fail')
  } finally {
    saving.value = false
  }
}

async function onPickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = '' // 允许连续选同一个文件
  if (!files.length) return

  uploading.value = true
  try {
    const res = await api.uploadTemplates(files)
    emit('templates-changed')
    if (res.errors?.length) {
      flash(`已上传 ${res.saved.length} 个；${res.errors.join('；')}`, res.saved.length ? 'ok' : 'fail')
    } else {
      flash(`已上传 ${res.saved.length} 个模板`)
    }
  } catch (e) {
    flash(e instanceof Error ? e.message : '上传失败', 'fail')
  } finally {
    uploading.value = false
  }
}

async function removeTemplate(item: TemplateItem) {
  if (!window.confirm(`删除模板「${item.name}」？已生成的图片不受影响。`)) return
  try {
    const res = await api.deleteTemplate(item.filename)
    if (res.ok) {
      emit('templates-changed')
      flash(`已删除「${item.name}」`)
    } else {
      flash(res.error || '删除失败', 'fail')
    }
  } catch (e) {
    flash(e instanceof Error ? e.message : '删除失败', 'fail')
  }
}

async function openDir() {
  try {
    const res = await api.openTemplateDir()
    if (!res.ok) flash(res.error || '打开目录失败', 'fail')
  } catch (e) {
    flash(e instanceof Error ? e.message : '打开目录失败', 'fail')
  }
}

function sizeText(bytes: number) {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
}
</script>

<template>
  <div>
    <div v-if="message" class="result" :class="messageType">{{ message }}</div>

    <div class="card">
      <h3>账号</h3>
      <div class="field">
        <label>你的姓名<span class="req">*</span></label>
        <input v-model="form.employee_name" type="text" placeholder="例如：张三" />
        <div class="hint">主管后台按这个姓名统计工作量，请与公司内部称呼一致</div>
      </div>
      <div class="field">
        <label>服务器地址<span class="req">*</span></label>
        <input v-model="form.server_url" type="text" placeholder="http://192.168.1.10:8080" />
        <div class="hint">
          当前状态：<span :style="{ color: serverOk ? '#059669' : '#dc2626' }">{{ serverOk ? '可连接' : '连不上' }}</span>
        </div>
      </div>
      <div class="field">
        <label>API Key<span class="req">*</span></label>
        <input v-model="form.api_key" type="password" placeholder="向主管索取" />
      </div>
    </div>

    <div class="card">
      <h3>PS 模板</h3>
      <div class="hint" style="margin-bottom: 9px">
        把 PSD 模板上传到这里，录用页的模板下拉框就从这些文件里选。可一次选多个。
      </div>

      <input
        ref="fileInput"
        type="file"
        accept=".psd"
        multiple
        style="display: none"
        @change="onPickFiles"
      />
      <div class="btnrow">
        <button class="btn" :disabled="uploading" @click="fileInput?.click()">
          {{ uploading ? '上传中…' : '上传 PSD 模板' }}
        </button>
        <button class="btn ghost" @click="openDir">打开模板目录</button>
      </div>

      <div v-if="templateCount" class="tpl-list">
        <div v-for="t in templates" :key="t.id" class="tpl-row">
          <span class="tpl-name">{{ t.name }}</span>
          <span class="tpl-size">{{ sizeText(t.size) }}</span>
          <button class="btn mini" @click="removeTemplate(t)">删除</button>
        </div>
      </div>
      <div v-else class="hint" style="margin-top: 9px">还没有模板，先上传一个 PSD 吧</div>

      <div class="field" style="margin-top: 12px">
        <label>模板目录</label>
        <input v-model="form.template_dir" type="text" placeholder="留空则用默认目录" />
        <div class="hint">一般不用改；要放共享盘统一更新时再改</div>
      </div>
    </div>

    <div class="card">
      <h3>合作伙伴</h3>
      <div class="field">
        <label>名单（一行一个）</label>
        <textarea v-model="partnersText" rows="5" placeholder="华东出版社&#10;南方文化"></textarea>
        <div class="hint">录用页的合作伙伴下拉框从这里取，按需要自己增删，保存后立即生效</div>
      </div>
    </div>

    <div class="card">
      <h3>生成设置</h3>
      <div class="field">
        <label>Photoshop 路径</label>
        <input v-model="form.ps_path" type="text" placeholder="留空则自动连接正在运行的 Photoshop" />
        <div class="hint">建议先手动打开 Photoshop 再生成，速度更快</div>
      </div>
      <div class="field">
        <label>输出目录</label>
        <input v-model="form.output_dir" type="text" placeholder="D:\期刊录用" />
      </div>
      <div class="row">
        <div class="field">
          <label>作者占位文字</label>
          <input v-model="form.author_placeholder" type="text" placeholder="{{ '{{作者}}' }}" />
          <div class="hint">PS 模板里代表作者的那段文字，生成时会换成填写的作者</div>
        </div>
        <div class="field">
          <label>标题占位文字</label>
          <input v-model="form.title_placeholder" type="text" placeholder="{{ '{{标题}}' }}" />
          <div class="hint">PS 模板里代表标题的那段文字，生成时会换成填写的标题</div>
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>JPG 质量（1-12）</label>
          <input v-model.number="form.jpg_quality" type="number" min="1" max="12" />
        </div>
        <div class="field">
          <label>生成后清空表单</label>
          <select v-model="form.auto_clear">
            <option :value="true">是</option>
            <option :value="false">否</option>
          </select>
        </div>
      </div>
    </div>

    <button class="btn" :disabled="saving" @click="save">
      {{ saving ? '保存中…' : '保存设置' }}
    </button>
  </div>
</template>
