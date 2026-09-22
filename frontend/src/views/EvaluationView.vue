<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  fetchEvaluationReport,
  type CaseDetail,
  type EvaluationReport,
  type MetricValue,
} from '@/api/evaluation'
import CaseDetailPanel from '@/components/CaseDetailPanel.vue'
import PageHeader from '@/components/PageHeader.vue'

/** 评估 Dashboard（Prompt 24.2）：七项指标可视化 + 错误案例清单 + 案例列表/单案例详情。
 *  数据源 = 后端 /api/evaluation/report（Prompt 22.3 评估报告 JSON，write_reports 落盘） */
const report = ref<EvaluationReport | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
const failuresOnly = ref(false)

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    report.value = await fetchEvaluationReport()
  } catch (e) {
    report.value = null
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 七项指标展示定义（顺序即呈现顺序，与后端 _METRIC_LABELS 对齐；lowerIsBetter 用于语义色） */
const METRICS = [
  { key: 'agent_routing_accuracy', title: 'Agent 路由准确率', lowerIsBetter: false },
  { key: 'tool_selection_accuracy', title: '工具选择准确率', lowerIsBetter: false },
  { key: 'task_completion_rate', title: '任务完成率', lowerIsBetter: false },
  { key: 'rag_groundedness', title: 'RAG 证据可溯源率', lowerIsBetter: false },
  { key: 'permission_violation_rate', title: '权限违规率', lowerIsBetter: true },
  { key: 'hitl_correctness', title: 'HITL 正确率', lowerIsBetter: false },
  { key: 'average_latency_ms', title: '平均延迟（ms）', lowerIsBetter: false },
] as const

/** 比率指标文案与语义色：lowerIsBetter 时达标为低值（违规率 0 最好） */
function metricDisplay(m: MetricValue, lowerIsBetter: boolean): { text: string; ok: boolean } {
  if (m.value === null) return { text: '—', ok: false }
  return {
    text: `${(m.value * 100).toFixed(1)}%`,
    ok: lowerIsBetter ? m.value === 0 : m.value >= 0.9,
  }
}

/** 案例列表：全部 / 仅错误案例 */
const cases = computed<CaseDetail[]>(() =>
  failuresOnly.value ? report.value?.failures ?? [] : report.value?.cases ?? [],
)
</script>

<template>
  <main class="container" v-loading="loading">
    <PageHeader title="评估 Dashboard" back-to="/" back-label="返回首页">
      <el-button size="small" @click="load">刷新</el-button>
    </PageHeader>

    <el-alert v-if="loadError" type="warning" :closable="false" class="card">
      {{ loadError }} —— 暂无评估报告，请先在 backend 目录执行
      <code>python -m app.evaluation.demo</code> 生成。
    </el-alert>

    <template v-if="report">
      <el-card shadow="never" class="card">
        <template #header>总体指标</template>
        <p class="summary muted">
          生成时间：{{ new Date(report.generated_at).toLocaleString() }} ｜ 案例总数：
          <strong>{{ report.metrics.total_cases }}</strong> ｜ 通过：
          <strong class="pass">{{ report.metrics.passed_cases }}</strong> ｜ 未通过：
          <strong class="fail">{{ report.failures.length }}</strong>
        </p>
        <div class="metrics">
          <div v-for="m in METRICS" :key="m.key" class="metric">
            <div class="metric-title">{{ m.title }}</div>
            <div class="metric-value" :class="{ ok: metricDisplay(report.metrics[m.key], m.lowerIsBetter).ok }">
              {{ metricDisplay(report.metrics[m.key], m.lowerIsBetter).text }}
            </div>
            <div class="muted">适用案例 n = {{ report.metrics[m.key].n }}</div>
          </div>
        </div>
      </el-card>

      <el-card shadow="never" class="card">
        <template #header>错误案例清单（{{ report.failures.length }}）</template>
        <el-table v-if="report.failures.length" :data="report.failures" size="small">
          <el-table-column label="案例" min-width="180">
            <template #default="{ row }">
              {{ row.case.id }}<br><span class="muted">{{ row.case.name }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="case.user_role" label="角色" width="110" />
          <el-table-column label="不匹配明细" min-width="280">
            <template #default="{ row }">
              <div v-for="(m, i) in row.result.mismatches" :key="i" class="mismatch">{{ m }}</div>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="无错误案例" :image-size="60" />
      </el-card>

      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-header">
            <span>案例列表（{{ cases.length }}）</span>
            <el-checkbox v-model="failuresOnly">仅看错误案例</el-checkbox>
          </div>
        </template>
        <el-table :data="cases" size="small" row-key="case.id">
          <el-table-column type="expand">
            <template #default="{ row }">
              <CaseDetailPanel :item="row as CaseDetail" class="expand" />
            </template>
          </el-table-column>
          <el-table-column label="案例" min-width="180">
            <template #default="{ row }">
              {{ row.case.id }}<br><span class="muted">{{ row.case.name }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="case.user_role" label="角色" width="110" />
          <el-table-column label="期望结果" width="170">
            <template #default="{ row }">
              {{ row.case.expected_outcome }}<br>
              <span class="muted">状态锚点：{{ row.case.expected_status }}</span>
            </template>
          </el-table-column>
          <el-table-column label="实际状态" width="140">
            <template #default="{ row }">
              {{ row.result.observation.status }}<br>
              <span class="muted">{{ row.result.latency_ms }} ms</span>
            </template>
          </el-table-column>
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <span :class="row.result.mismatches.length === 0 ? 'pass' : 'fail'">
                {{ row.result.mismatches.length === 0 ? '通过' : '未通过' }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </main>
</template>

<style scoped>
.container {
  max-width: 1080px;
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
  width: 100%;
}
.summary {
  margin-top: 0;
}
.metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.metric {
  flex: 1 1 180px;
  min-width: 180px;
  padding: 12px 16px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  text-align: center;
}
.metric-title {
  font-size: 0.9em;
  color: #4b5563;
}
.metric-value {
  font-size: 1.8em;
  font-weight: 700;
  color: #dc2626;
  margin: 4px 0;
}
.metric-value.ok {
  color: #16a34a;
}
.muted {
  color: #909399;
  font-size: 0.9em;
}
.pass {
  color: #16a34a;
  font-weight: 600;
}
.fail {
  color: #dc2626;
  font-weight: 600;
}
.mismatch {
  color: #cf222e;
  font-size: 0.9em;
}
.expand {
  padding: 4px 16px;
}
</style>
