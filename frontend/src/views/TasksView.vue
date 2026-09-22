<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { cancelTask, fetchTasks, taskStatusLabel, TASK_STATUS_TYPES, type TaskResponse } from '@/api/tasks'
import { extractErrorMessage } from '@/api/client'
import PageHeader from '@/components/PageHeader.vue'

/** 任务列表页：本人任务（创建时间倒序），支持取消未完成任务；结果展示见 /analysis */

const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

const router = useRouter()
const loading = ref(false)
const tasks = ref<TaskResponse[]>([])

async function refresh(): Promise<void> {
  loading.value = true
  try {
    tasks.value = await fetchTasks()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

async function onCancel(task: TaskResponse): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定取消任务「${task.title}」？`, '取消任务', { type: 'warning' })
  } catch {
    return // 用户点消确认框
  }
  try {
    await cancelTask(task.id)
    ElMessage.success('已取消')
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
    <PageHeader title="我的任务" back-to="/" back-label="返回首页">
      <el-button size="small" @click="router.push('/agent')">新建任务</el-button>
      <el-button size="small" :loading="loading" @click="refresh">刷新</el-button>
    </PageHeader>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="tasks" empty-text="暂无任务，点击右上角新建">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="TASK_STATUS_TYPES[row.status] ?? 'info'">
              {{ taskStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="完成时间" width="180">
          <template #default="{ row }">
            {{ row.finished_at ? formatTime(row.finished_at) : '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/tasks/${row.id}`)">
              详情
            </el-button>
            <el-button
              v-if="row.status === 'waiting_approval'"
              link
              type="warning"
              @click="router.push('/approvals')"
            >
              去审批
            </el-button>
            <el-button
              v-if="!TERMINAL.has(row.status)"
              link
              type="danger"
              @click="onCancel(row as TaskResponse)"
            >
              取消
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </main>
</template>

<style scoped>
.container {
  max-width: 900px;
  margin: 32px auto;
  padding: 0 16px;
}
</style>
