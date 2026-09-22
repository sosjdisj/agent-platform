<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  fetchTracePage,
  TASK_EVENT_LABELS,
  TASK_STATUS_LABELS,
  TASK_STATUS_TYPES,
  type TraceEvent,
} from '@/api/tasks'

/**
 * Trace 时间线（23.4）：数据源为轨迹回放 REST（与 SSE 同源于 agent_trace_events），
 * keyset 分页（加载更早），父组件可递增 revision 触发最新页增量合并（实时事件落地后刷新）。
 * 仅展示 Agent / 工具 / 耗时 / 状态 / 错误等结构化信息，不展示 CoT（LLM 调用不渲染摘要）。
 */

const props = defineProps<{ taskId: number; revision?: number }>()

const PAGE_SIZE = 50

const loading = ref(false)
const loadingEarlier = ref(false)
const exhausted = ref(false)
// 存储按 id 倒序（最新在前）；渲染时反转为时间升序
const items = ref<TraceEvent[]>([])

const oldestId = computed(() =>
  items.value.length ? Math.min(...items.value.map((i) => i.id ?? 0)) : undefined,
)

/** 时间升序视图：回放条目按 timestamp 排序，SSE 帧兜底按 id / 到达序 */
const chronological = computed(() =>
  [...items.value].sort((a, b) => {
    if (a.id != null && b.id != null) return a.id - b.id
    if (a.timestamp && b.timestamp) return a.timestamp.localeCompare(b.timestamp)
    return 0
  }),
)

function merge(page: TraceEvent[]): void {
  const known = new Set(items.value.filter((i) => i.id != null).map((i) => i.id))
  items.value = [...items.value, ...page.filter((i) => i.id != null && !known.has(i.id))]
  items.value.sort((a, b) => (b.id ?? 0) - (a.id ?? 0))
}

async function loadLatest(): Promise<void> {
  loading.value = true
  try {
    merge(await fetchTracePage(props.taskId))
  } catch (e) {
    items.value = [] // 归属变化（403/404）等场景清空，错误由调用方页面提示
  } finally {
    loading.value = false
  }
}

async function loadEarlier(): Promise<void> {
  if (oldestId.value == null) return
  loadingEarlier.value = true
  try {
    const page = await fetchTracePage(props.taskId, oldestId.value, PAGE_SIZE)
    merge(page)
    exhausted.value = page.length < PAGE_SIZE
  } catch (e) {
    exhausted.value = true
  } finally {
    loadingEarlier.value = false
  }
}

watch(
  () => props.revision,
  () => {
    void loadLatest()
  },
)

onMounted(loadLatest)

function label(event: TraceEvent): string {
  return TASK_EVENT_LABELS[event.event] ?? event.event
}

function detail(event: TraceEvent): string {
  if (event.event === 'llm_call') return '' // 不展示 CoT：LLM 调用仅保留结构性信息
  const text = event.result_summary || event.parameter_summary || ''
  // 兜底：历史轨迹的 result_summary 可能是旧版原始 JSON dump，不渲染行级数据
  if (event.event === 'tool_result' && /^[{[]/.test(text)) return ''
  return text
}

function formatTime(iso?: string): string {
  return iso ? new Date(iso).toLocaleString() : ''
}

const COLORS: Record<string, string> = {
  task_started: '#409eff',
  agent_selected: '#409eff',
  agent_finished: '#67c23a',
  tool_called: '#e6a23c',
  tool_result: '#67c23a',
  permission_denied: '#f56c6c',
  approval_required: '#e6a23c',
  approval_result: '#909399',
  task_completed: '#67c23a',
  task_failed: '#f56c6c',
  task_cancelled: '#909399',
}
</script>

<template>
  <div v-loading="loading" class="timeline-wrap">
    <el-empty v-if="!chronological.length && !loading" description="暂无轨迹" :image-size="60" />
    <el-timeline v-else class="timeline">
      <el-timeline-item
        v-for="(e, i) in chronological"
        :key="e.id ?? `live-${i}`"
        :color="COLORS[e.event] ?? '#909399'"
        :timestamp="formatTime(e.timestamp)"
      >
        <div class="head">
          <span class="name">{{ label(e) }}</span>
          <span v-if="e.agent" class="dim">{{ e.agent }}</span>
          <span v-if="e.tool" class="dim">· {{ e.tool }}</span>
          <span v-if="e.round != null" class="dim">· 第 {{ e.round }} 轮</span>
          <span v-if="e.duration_ms != null" class="dim">· {{ e.duration_ms }}ms</span>
          <el-tag v-if="e.status" size="small" :type="TASK_STATUS_TYPES[e.status] ?? 'info'">
            {{ TASK_STATUS_LABELS[e.status] ?? e.status }}
          </el-tag>
          <el-tag v-if="e.error_code" size="small" type="danger">{{ e.error_code }}</el-tag>
        </div>
        <div v-if="detail(e)" class="body">{{ detail(e) }}</div>
      </el-timeline-item>
    </el-timeline>

    <el-button
      v-if="!exhausted && items.length >= PAGE_SIZE"
      class="more"
      size="small"
      :loading="loadingEarlier"
      @click="loadEarlier"
    >
      加载更早事件
    </el-button>
  </div>
</template>

<style scoped>
.timeline-wrap {
  min-height: 80px;
}
.timeline {
  padding-left: 4px;
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.name {
  font-weight: 600;
}
.dim {
  color: #909399;
  font-size: 0.85em;
}
.body {
  margin-top: 4px;
  color: #606266;
  font-size: 0.88em;
  word-break: break-all;
}
.more {
  margin-top: 8px;
}
</style>
