<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { decideApproval, fetchApprovals, RISK_LABELS, RISK_TYPES } from '@/api/approvals'
import { extractErrorMessage } from '@/api/client'
import type { TraceEvent } from '@/api/tasks'
import type { useTaskStream } from '@/composables/useTaskStream'

/**
 * HITL 审批弹窗：Agent 请求高危操作时就地弹出批准 / 拒绝，免跳转审批中心
 * （审批中心页面保留，作为历史记录与兜底入口）。触发源两路合一、按 approval_id 去重：
 * ① SSE approval_required 帧（即时）；② 任务推进到 waiting_approval 时查审批列表兜底
 * （Redis Pub/Sub 无回放，订阅晚于事件会丢帧）。收到 approval_result（含他处已处理）自动关闭。
 */

type Stream = ReturnType<typeof useTaskStream>

const props = defineProps<{ stream: Stream }>()

const visible = ref(false)
const submitting = ref(false)
const comment = ref('')
const current = ref<{
  approvalId: number
  taskTitle: string
  tool: string
  riskLevel: string | null
  parameterSummary: string | null
} | null>(null)

/** 已弹出过的审批 id：SSE 帧与状态兜底可能命中同一条，避免重复弹窗 */
const seen = new Set<number>()

/** 兼容两种载荷：SSE 帧（approval_id）与 GET /approvals 列表项（id / task_title） */
function open(payload: {
  approval_id?: number
  id?: number
  task_id: number
  task_title?: string | null
  tool?: string | null
  risk_level?: string | null
  parameter_summary?: string | null
}): void {
  const approvalId = payload.approval_id ?? payload.id
  if (approvalId === undefined || seen.has(approvalId)) return
  seen.add(approvalId)
  current.value = {
    approvalId,
    taskTitle: payload.task_title ?? `任务 #${payload.task_id}`,
    tool: payload.tool ?? '未知工具',
    riskLevel: payload.risk_level ?? null,
    parameterSummary: payload.parameter_summary ?? null,
  }
  comment.value = ''
  visible.value = true
}

async function decide(decision: 'approved' | 'rejected'): Promise<void> {
  if (!current.value) return
  submitting.value = true
  try {
    await decideApproval(current.value.approvalId, decision, comment.value.trim() || undefined)
    ElMessage.success(decision === 'approved' ? '已批准，任务继续执行' : '已拒绝，任务终止')
    visible.value = false
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    submitting.value = false
  }
}

function handleEvent(event: TraceEvent): void {
  if (event.event === 'approval_required') {
    open(event)
  } else if (
    event.event === 'approval_result' &&
    current.value &&
    event.approval_id === current.value.approvalId
  ) {
    visible.value = false // 已在别处（如审批中心）处理，关闭弹窗
  }
}

/** 兜底：SSE 丢帧场景（如详情页打开时任务已在等待审批），状态推进时拉审批列表就地弹出 */
const stopWatch = watch(
  () => props.stream.task.value?.status,
  async (status) => {
    if (status !== 'waiting_approval') return
    const taskId = props.stream.task.value?.id
    if (taskId === undefined) return
    try {
      const items = await fetchApprovals()
      const pending = items.find((a) => a.task_id === taskId && a.status === 'pending')
      if (pending) open(pending)
    } catch {
      // 列表拉取失败不阻塞主流程，仍可去审批中心处理
    }
  },
)

const unsubscribe = props.stream.onStreamEvent(handleEvent)
onBeforeUnmount(() => {
  stopWatch()
  unsubscribe()
})
</script>

<template>
  <el-dialog v-model="visible" title="需要人工审批" width="480px" :close-on-click-modal="false">
    <template v-if="current">
      <p class="lead">
        Agent 请求执行高危操作「<strong>{{ current.tool }}</strong>」，批准后任务才会继续执行。
      </p>
      <div class="meta">
        <span class="task">{{ current.taskTitle }}</span>
        <el-tag size="small" type="info">{{ current.tool }}</el-tag>
        <el-tag v-if="current.riskLevel" size="small" :type="RISK_TYPES[current.riskLevel] ?? 'info'">
          {{ RISK_LABELS[current.riskLevel] ?? current.riskLevel }}
        </el-tag>
      </div>
      <p class="params">参数：{{ current.parameterSummary ?? '—' }}</p>
      <el-input v-model="comment" type="textarea" :rows="2" placeholder="拒绝原因（可选，拒绝时生效）" />
    </template>
    <template #footer>
      <el-button :disabled="submitting" @click="visible = false">稍后处理</el-button>
      <el-button type="danger" :loading="submitting" @click="decide('rejected')">拒绝</el-button>
      <el-button type="primary" :loading="submitting" @click="decide('approved')">批准</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.lead {
  margin: 0 0 10px;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.task {
  font-weight: 600;
}
.params {
  margin: 8px 0 12px;
  font-family: monospace;
  font-size: 0.85em;
  word-break: break-all;
  color: #606266;
}
</style>
