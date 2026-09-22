<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createTask, TASK_EVENT_LABELS, taskStatusLabel, TASK_STATUS_TYPES, type TraceEvent } from '@/api/tasks'
import { extractErrorMessage } from '@/api/client'
import { useTaskStream } from '@/composables/useTaskStream'
import ReportPanel from '@/components/ReportPanel.vue'
import PageHeader from '@/components/PageHeader.vue'

/** 核心场景：提交分析任务 → 实时事件流（SSE）→ 九部分结构化报告展示 */

const DEFAULT_QUERY = '帮我分析客户 A 最近销售额下降的原因'

const stream = useTaskStream()
const { task, events, report, isTerminal } = stream

const query = ref(DEFAULT_QUERY)
const submitting = ref(false)

async function onSubmit(): Promise<void> {
  submitting.value = true
  try {
    const text = query.value.trim()
    stream.reset()
    await stream.update(await createTask(text.slice(0, 200), text))
    ElMessage.success('任务已提交')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    submitting.value = false
  }
}

async function onCancel(): Promise<void> {
  try {
    await stream.cancel()
    ElMessage.success('已取消')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

function eventLabel(event: TraceEvent): string {
  const parts = [TASK_EVENT_LABELS[event.event] ?? event.event]
  if (event.agent) parts.push(event.agent)
  if (event.tool) parts.push(event.tool)
  return parts.join(' · ')
}
</script>

<template>
  <main class="container">
    <PageHeader title="客户智能分析" back-to="/" back-label="返回首页" />

    <el-card shadow="never" class="card">
      <template #header>提交分析任务</template>
      <el-input
        v-model="query"
        type="textarea"
        :rows="2"
        placeholder="输入分析任务，例如：帮我分析客户 A 最近销售额下降的原因"
      />
      <div class="actions">
        <el-button
          type="primary"
          :loading="submitting"
          :disabled="!!task && !isTerminal"
          @click="onSubmit"
        >
          开始分析
        </el-button>
        <el-button v-if="task && !isTerminal" @click="onCancel">取消任务</el-button>
        <el-tag v-if="task" :type="TASK_STATUS_TYPES[task.status] ?? 'info'">
          {{ taskStatusLabel(task.status) }}
        </el-tag>
      </div>
      <p class="hint">任务将按需调度数据查询与知识检索 Agent，全程事件实时推送。</p>
    </el-card>

    <el-card v-if="task" shadow="never" class="card">
      <template #header>执行事件（实时）</template>
      <el-empty v-if="!events.length" description="等待事件..." :image-size="60" />
      <el-timeline v-else class="timeline">
        <el-timeline-item v-for="(e, i) in events" :key="i" :type="e.event === 'task_completed' ? 'success' : undefined">
          <div class="event-head">{{ eventLabel(e) }}</div>
          <div v-if="e.result_summary" class="event-body">{{ e.result_summary }}</div>
          <div v-else-if="e.parameter_summary" class="event-body muted">{{ e.parameter_summary }}</div>
        </el-timeline-item>
      </el-timeline>
    </el-card>

    <el-alert v-if="task?.error" :title="task.error" type="error" :closable="false" class="card" />

    <el-card v-if="report" shadow="never" class="card">
      <template #header>分析报告</template>
      <ReportPanel :report="report" />
    </el-card>
  </main>
</template>

<style scoped>
.container {
  max-width: 760px;
  margin: 32px auto;
  padding: 0 16px;
}
.card {
  margin-bottom: 16px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.hint {
  margin: 8px 0 0;
  color: #909399;
  font-size: 0.85em;
}
.timeline {
  padding-left: 4px;
}
.event-head {
  font-weight: 600;
}
.event-body {
  color: #606266;
  font-size: 0.88em;
  word-break: break-all;
}
.muted {
  color: #909399;
  font-size: 0.85em;
}
</style>
