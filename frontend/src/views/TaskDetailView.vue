<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useTaskStream } from '@/composables/useTaskStream'
import ApprovalDialog from '@/components/ApprovalDialog.vue'
import ReportPanel from '@/components/ReportPanel.vue'
import TraceTimeline from '@/components/TraceTimeline.vue'
import { taskStatusLabel, TASK_STATUS_TYPES, type TraceEvent } from '@/api/tasks'
import { extractErrorMessage } from '@/api/client'
import PageHeader from '@/components/PageHeader.vue'

/** 任务详情：订阅 SSE 实时展示执行轨迹与状态，数据全部来自后端事件，页面不预置流程 */

const route = useRoute()
const router = useRouter()
// SSE 帧到 → 去抖递增 revision，TraceTimeline 增量刷新最新轨迹页（与 Trace 表同源）
let bumpPending = false
function bumpRevision(): void {
  if (bumpPending) return
  bumpPending = true
  window.setTimeout(() => {
    bumpPending = false
    revision.value++
  }, 600)
}

const stream = useTaskStream(bumpRevision)
const { task, events, report, isTerminal } = stream

const loading = ref(true)
const revision = ref(0)

function latestEvent(name: string): TraceEvent | undefined {
  const list = events.value
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].event === name) return list[i]
  }
  return undefined
}

/** 当前 Agent / 工具 = 最近一次 agent_selected / tool_called 事件（非终态时展示） */
const currentAgent = computed(() => latestEvent('agent_selected')?.agent ?? null)
const currentTool = computed(() => latestEvent('tool_called')?.tool ?? null)

async function onCancel(): Promise<void> {
  try {
    await stream.cancel()
    ElMessage.success('已取消')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

onMounted(async () => {
  const id = Number(route.params.id)
  if (!Number.isInteger(id) || id <= 0) {
    ElMessage.error('任务 ID 非法')
    await router.replace('/tasks')
    return
  }
  try {
    await stream.open(id)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
    await router.replace('/tasks')
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main v-loading="loading" class="container">
    <PageHeader title="任务详情" back-to="/tasks" back-label="返回列表" />

    <el-card v-if="task" shadow="never" class="card">
      <template #header>任务概览</template>
      <div class="meta">
        <span class="muted">ID</span><strong>#{{ task.id }}</strong>
        <span class="muted">标题</span><strong>{{ task.title }}</strong>
        <span class="muted">状态</span>
        <el-tag :type="TASK_STATUS_TYPES[task.status] ?? 'info'">
          {{ taskStatusLabel(task.status) }}
        </el-tag>
      </div>
      <p class="query">查询：{{ task.query }}</p>
    </el-card>

    <el-alert
      v-if="task?.status === 'waiting_approval'"
      type="warning"
      show-icon
      :closable="false"
      class="card"
    >
      <template #title>任务正在等待高危操作审批，需在审批中心批准后才能继续</template>
      <el-button size="small" type="primary" @click="router.push('/approvals')">
        去审批中心处理
      </el-button>
    </el-alert>

    <el-card v-if="task" shadow="never" class="card">
      <template #header>
        <div class="card-header">
          <span>当前执行</span>
          <el-button v-if="!isTerminal" size="small" type="danger" @click="onCancel">
            取消任务
          </el-button>
        </div>
      </template>
      <div class="current-row">
        <div>
          <span class="muted">当前 Agent：</span>
          <strong>{{ currentAgent ?? '—' }}</strong>
        </div>
        <div>
          <span class="muted">当前工具：</span>
          <strong>{{ currentTool ?? '—' }}</strong>
        </div>
      </div>
      <p class="hint muted">以下轨迹由后端事件实时推送。</p>
    </el-card>

    <el-card v-if="task" shadow="never" class="card">
      <template #header>执行轨迹</template>
      <TraceTimeline :task-id="Number(route.params.id)" :revision="revision" />
    </el-card>

    <el-alert v-if="task?.error" :title="task.error" type="error" :closable="false" class="card" />

    <el-card v-if="report" shadow="never" class="card">
      <template #header>分析报告</template>
      <ReportPanel :report="report" />
    </el-card>

    <!-- Agent 请求高危操作时就地弹窗审批，免跳转审批中心 -->
    <ApprovalDialog :stream="stream" />
  </main>
</template>

<style scoped>
.container {
  max-width: 960px;
  margin: 32px auto;
  padding: 0 16px;
}
.card {
  margin-bottom: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 16px;
}
.query {
  margin: 10px 0 0;
  word-break: break-all;
}
.current-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
}
.hint {
  margin: 10px 0 0;
}
.muted {
  color: #909399;
  font-size: 0.85em;
}
</style>
