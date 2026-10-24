<script setup lang="ts">
import { computed } from 'vue'

/**
 * 记录区的非列表态：加载骨架 / 服务器错误 / 一条没有 / 被筛没了。
 * 优先级与原先的 v-if 链一致：loading > error > 空列表 > 筛空。
 */
const props = defineProps<{
  loading: boolean
  error: string
  /** 服务端一条记录都没返回 */
  emptyTotal: boolean
  /** 有记录，但被关键词/下拉/日期筛得一条不剩 */
  emptyFiltered: boolean
  /** 当前是否自定义区间（空态里提示"换个区间"用） */
  custom: boolean
  /** 快捷区间档位（非 all 时补一句提示） */
  range: string
}>()
const emit = defineEmits<{ reload: []; 'clear-filters': [] }>()

const hasError = computed(() => !props.loading && !!props.error)
const isEmpty = computed(() => !props.loading && !props.error && props.emptyTotal)
const isFiltered = computed(() => !props.loading && !props.error && !props.emptyTotal && props.emptyFiltered)
</script>

<template>
  <!-- 骨架屏：避免"正在加载…"白屏感 -->
  <div v-if="loading" class="card">
    <div class="skeleton">
      <div class="sk thumb" />
      <div class="sk lines">
        <div class="sk line w60" />
        <div class="sk line w40" />
      </div>
    </div>
    <div class="skeleton">
      <div class="sk thumb" />
      <div class="sk lines">
        <div class="sk line w40" />
        <div class="sk line w60" />
      </div>
    </div>
    <div class="skeleton">
      <div class="sk thumb" />
      <div class="sk lines">
        <div class="sk line w60" />
        <div class="sk line w40" />
      </div>
    </div>
  </div>

  <div v-else-if="hasError" class="result fail">
    <div class="head"><span class="ico">!</span>{{ error }}</div>
    <div>可能是服务器离线。记录不会丢，恢复后点底部「立即补传」。</div>
    <button class="btn ghost sm" style="margin-top: 9px" @click="emit('reload')">重新加载</button>
  </div>

  <div v-else-if="isEmpty" class="empty">
    <span class="e-ico">
      <svg viewBox="0 0 16 16" width="19" height="19">
        <rect x="2.2" y="2.2" width="11.6" height="11.6" rx="2.2" fill="none" stroke="currentColor" stroke-width="1.3" />
        <path d="M5 8.2l2 2 4-4.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </span>
    <span class="e-main">还没有录用记录</span>
    <span class="e-sub">
      回到主窗口，选模板、填作者和标题，点生成。记录会自动出现在这里。
      <template v-if="custom || range !== 'all'">换个时间区间，或点「全部」看更早的记录。</template>
    </span>
  </div>

  <div v-else-if="isFiltered" class="empty">
    <span class="e-ico">
      <svg viewBox="0 0 16 16" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round">
        <circle cx="7" cy="7" r="4.2" />
        <line x1="10.2" y1="10.2" x2="13.6" y2="13.6" />
      </svg>
    </span>
    <span class="e-main">没有匹配的记录</span>
    <span class="e-sub">换个关键词，或清掉上面的筛选条件试试。</span>
    <button class="btn ghost sm" @click="emit('clear-filters')">清空筛选</button>
  </div>
</template>
