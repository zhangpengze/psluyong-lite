<script setup lang="ts">
import { ref } from 'vue'
import { api, type TemplateItem } from '../../api/sidecar'
import { useConfirm } from '../../composables/useConfirm'
import { useToast } from '../../composables/useToast'

/** 设置 · 模板分组：PSD 上传（点选 + 拖拽）、列表与删除 */
const props = defineProps<{
  templates: TemplateItem[]
}>()
const emit = defineEmits<{ 'templates-changed': [] }>()

const { confirm } = useConfirm()
const { toast } = useToast()

const uploading = ref(false)
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

function flash(text: string, type: 'success' | 'warning' | 'error' = 'success') {
  toast(text, type)
}

// ---------- PSD 上传：按钮选择 + 拖拽 ----------
function pickFiles() {
  fileInput.value?.click()
}

async function uploadFiles(files: File[]) {
  const psd = files.filter((f) => f.name.toLowerCase().endsWith('.psd'))
  if (psd.length < files.length) {
    flash(`已忽略 ${files.length - psd.length} 个非 PSD 文件`, 'warning')
  }
  if (!psd.length) return

  uploading.value = true
  try {
    const res = await api.uploadTemplates(psd)
    emit('templates-changed')
    if (res.errors?.length) {
      flash(`已上传 ${res.saved.length} 个；${res.errors.join('；')}`, res.saved.length ? 'warning' : 'error')
    } else {
      flash(`已上传 ${res.saved.length} 个模板`, 'success')
    }
  } catch (e) {
    flash(e instanceof Error ? e.message : '上传失败', 'error')
  } finally {
    uploading.value = false
  }
}

function onPickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = '' // 允许连续选同一个文件
  void uploadFiles(files)
}

function onDragOver(e: DragEvent) {
  e.preventDefault()
  dragging.value = true
}
function onDragLeave() {
  dragging.value = false
}
function onDrop(e: DragEvent) {
  e.preventDefault()
  dragging.value = false
  const files = Array.from(e.dataTransfer?.files ?? [])
  if (files.length) void uploadFiles(files)
}

async function removeTemplate(item: TemplateItem) {
  const ok = await confirm({
    title: '删除模板',
    message: `删除模板「${item.name}」？已生成的图片不受影响。`,
    danger: true,
    confirmText: '删除',
  })
  if (!ok) return
  try {
    const res = await api.deleteTemplate(item.filename)
    if (res.ok) {
      emit('templates-changed')
      flash(`已删除「${item.name}」`, 'success')
    } else {
      flash(res.error || '删除失败', 'error')
    }
  } catch (e) {
    flash(e instanceof Error ? e.message : '删除失败', 'error')
  }
}

async function openDir() {
  try {
    const res = await api.openTemplateDir()
    if (!res.ok) flash(res.error || '打开目录失败', 'error')
  } catch (e) {
    flash(e instanceof Error ? e.message : '打开目录失败', 'error')
  }
}

function sizeText(bytes: number) {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
}
</script>

<template>
  <input ref="fileInput" type="file" accept=".psd" multiple style="display: none" @change="onPickFiles" />
  <div
    class="dropzone"
    :class="{ drag: dragging }"
    role="button"
    tabindex="0"
    @click="pickFiles"
    @keydown.enter="pickFiles"
    @keydown.space.prevent="pickFiles"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <span class="dz-ico">
      <svg viewBox="0 0 16 16" width="16" height="16">
        <path d="M8 11V3.8M4.8 7L8 3.2 11.2 7" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
        <path d="M2.6 11.4v1.2a1.4 1.4 0 0 0 1.4 1.4h8a1.4 1.4 0 0 0 1.4-1.4v-1.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
      </svg>
    </span>
    <span>{{ uploading ? '上传中…' : '点击选择 PSD 文件，或拖拽到此处' }}</span>
  </div>

  <div v-if="templates.length" class="tpl-list" style="margin-top: 9px">
    <div v-for="t in templates" :key="t.id" class="tpl-row">
      <span class="tpl-name" :title="t.name">{{ t.name }}</span>
      <span class="tpl-size">{{ sizeText(t.size) }}</span>
      <button
        class="tpl-del"
        :title="`删除模板「${t.name}」`"
        :aria-label="`删除模板 ${t.name}`"
        @click="removeTemplate(t)"
      >
        <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
          <path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6" />
        </svg>
      </button>
    </div>
  </div>
  <div v-else class="hint" style="margin-top: 8px">还没有模板，先上传一个 PSD 吧</div>

  <div class="tpl-foot">
    <button type="button" class="link" @click="openDir">打开模板目录</button>
  </div>
</template>
