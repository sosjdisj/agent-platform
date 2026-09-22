<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  APPROVAL_STATUS_LABELS,
  APPROVAL_STATUS_TYPES,
  decideApproval,
  fetchApprovals,
  RISK_LABELS,
  RISK_TYPES,
  type ApprovalDecision,
  type ApprovalItem,
} from '@/api/approvals'
import { extractErrorMessage } from '@/api/client'
import PageHeader from '@/components/PageHeader.vue'

/** 审批中心：HIGH 风险操作的待审批卡片（数据来自后端真实 interrupt），批准 → 任务继续，拒绝 → 任务终止 */

const router = useRouter()
const loading = ref(false)
const approvals = ref<ApprovalItem[]>([])

// 待审批在前，其余按时间倒序（后端已按 id 倒序返回，此处仅把 pending 提前）
const ordered = computed(() => {
  const pending = approvals.value.filter((a) => a.status === 'pending')
  const decided = approvals.value.filter((a) => a.status !== 'pending')
  return [...pending, ...decided]
})

async function refresh(): Promise<void> {
  loading.value = true
  try {
    approvals.value = await fetchApprovals()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

async function onDecide(item: ApprovalItem, decision: ApprovalDecision): Promise<void> {
  const title = `${decision === 'approved' ? '批准' : '拒绝'}「${item.tool}」？`
  let comment: string | undefined
  try {
    if (decision === 'approved') {
      await ElMessageBox.confirm('批准后任务将从中断点继续执行该操作。', title, { type: 'warning' })
    } else {
      const { value } = await ElMessageBox.prompt('拒绝后任务将终止，可填写拒绝原因。', title, {
        type: 'warning',
        inputPlaceholder: '拒绝原因（可选）',
      })
      comment = value || undefined
    }
  } catch {
    return // 用户点消确认框
  }
  try {
    await decideApproval(item.id, decision, comment)
    ElMessage.success(decision === 'approved' ? '已批准，任务继续执行' : '已拒绝，任务终止')
    await refresh()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString()
}

onMounted(refresh)
</script>

<template>
  <main class="container">
    <PageHeader title="审批中心" back-to="/" back-label="返回首页">
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </PageHeader>

    <el-empty v-if="!ordered.length && !loading" description="暂无审批记录" />

    <el-card
      v-for="item in ordered"
      :key="item.id"
      shadow="never"
      class="card"
      :class="{ pending: item.status === 'pending' }"
    >
      <div class="row">
        <div class="info">
          <div class="title-line">
            <strong>{{ item.task_title ?? `任务 #${item.task_id}` }}</strong>
            <el-tag size="small" type="info">{{ item.tool }}</el-tag>
            <el-tag v-if="item.risk_level" size="small" :type="RISK_TYPES[item.risk_level] ?? 'info'">
              {{ RISK_LABELS[item.risk_level] ?? item.risk_level }}
            </el-tag>
            <el-tag size="small" :type="APPROVAL_STATUS_TYPES[item.status] ?? 'info'">
              {{ APPROVAL_STATUS_LABELS[item.status] ?? item.status }}
            </el-tag>
          </div>
          <p class="params">参数：{{ item.parameter_summary ?? '—' }}</p>
          <p class="meta">
            申请人 #{{ item.requester_id }} · 申请于 {{ formatTime(item.created_at) }}
            <template v-if="item.decided_at">
              · {{ APPROVAL_STATUS_LABELS[item.status] }}于 {{ formatTime(item.decided_at) }}
            </template>
            <template v-if="item.comment"> · 意见：{{ item.comment }}</template>
          </p>
        </div>
        <div class="actions">
          <template v-if="item.status === 'pending'">
            <el-button type="primary" size="small" @click="onDecide(item, 'approved')">
              批准
            </el-button>
            <el-button type="danger" size="small" @click="onDecide(item, 'rejected')">
              拒绝
            </el-button>
          </template>
          <el-button size="small" @click="router.push(`/tasks/${item.task_id}`)">
            查看任务
          </el-button>
        </div>
      </div>
    </el-card>
  </main>
</template>

<style scoped>
.container {
  max-width: 860px;
  margin: 32px auto;
  padding: 0 16px;
}
.card {
  margin-bottom: 12px;
}
.card.pending {
  border-left: 3px solid #e6a23c;
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.title-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.params {
  margin: 8px 0 0;
  font-family: monospace;
  font-size: 0.85em;
  word-break: break-all;
  color: #606266;
}
.meta {
  margin: 6px 0 0;
  color: #909399;
  font-size: 0.85em;
}
.actions {
  display: flex;
  flex-shrink: 0;
}
</style>
