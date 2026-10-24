<script setup lang="ts">
import { computed } from 'vue'
import type { ClientConfig } from '../../api/sidecar'

/** 设置 · 生成分组：JPG 质量 / 命名规则 / 生成后清空 / 合作伙伴名单 */
const props = defineProps<{
  form: ClientConfig
}>()
/** 名单用「一行一个」的文本框维护（内容归 SettingsPanel：保存与 dirty 判定都要它） */
const partnersText = defineModel<string>('partnersText', { required: true })

/** 图片命名：只留两个选项，示例跟着选中的那个走，比两张各自带例子的卡片省 70px */
const FILENAME_OPTIONS = [
  { value: 'author', label: '作者姓名', sample: '李四.jpg' },
  { value: 'author_title', label: '作者 + 标题', sample: '李四_基于深度学习的图像分割方法研究.jpg' },
] as const

const filenameSample = computed(
  () => FILENAME_OPTIONS.find((o) => o.value === props.form.filename_rule)?.sample ?? FILENAME_OPTIONS[0].sample,
)
</script>

<template>
  <div class="field">
    <label>JPG 质量（1-12）</label>
    <div class="range-row">
      <input v-model.number="form.jpg_quality" type="range" min="1" max="12" step="1" />
      <span class="range-val">{{ form.jpg_quality }}</span>
    </div>
    <div class="hint">数字越大越清晰、文件也越大；10 以上肉眼差别不大</div>
  </div>
  <div class="field">
    <label>图片命名</label>
    <div class="chip-row">
      <button
        v-for="opt in FILENAME_OPTIONS"
        :key="opt.value"
        class="range-chip"
        :class="{ active: form.filename_rule === opt.value }"
        @click="form.filename_rule = opt.value"
      >
        {{ opt.label }}
      </button>
    </div>
    <div class="hint">例如 <span class="mono">{{ filenameSample }}</span>；同名图片自动加 (2)、(3)，不覆盖已生成的图</div>
  </div>
  <div class="field">
    <label>生成后清空表单</label>
    <label class="switch">
      <input v-model="form.auto_clear" type="checkbox" />
      <span class="sw" />
      <span>{{ form.auto_clear ? '开' : '关' }}</span>
    </label>
    <div class="hint">开：生成成功后自动清空作者和标题，方便连续录入；关：保留内容便于修改重试</div>
  </div>
  <div class="field">
    <label>合作伙伴名单（一行一个）</label>
    <textarea v-model="partnersText" rows="4" placeholder="华东出版社&#10;南方文化" />
    <div class="hint">录用页的合作伙伴从这里选，按需要自己增删，保存后生效</div>
  </div>
</template>
