<script setup lang="ts">
import type { CaseDetail } from '@/api/evaluation'

/** 单案例详情（24.2 Dashboard）：期望（案例声明）× 判定结果 × 实际观察，逐维度对错可视化 */
defineProps<{ item: CaseDetail }>()
</script>

<template>
  <section class="case-detail">
    <h3>{{ item.case.name }}<span class="muted">（{{ item.case.id }}）</span></h3>
    <p class="muted">角色：{{ item.case.user_role }} | 延迟 {{ item.result.latency_ms }} ms | 结果：
      <span :class="item.result.observation.status === item.case.expected_status ? 'pass' : 'fail'">
        {{ item.result.observation.status }}
      </span>
    </p>

    <el-descriptions :column="2" border size="small">
      <el-descriptions-item label="用户输入" :span="2">{{ item.case.input }}</el-descriptions-item>
      <el-descriptions-item label="路由 Agent">
        <span class="muted">期望：{{ item.case.expected_agents.join('、') || '—' }}</span><br>
        <span>实际：{{ item.result.observation.agents.join('、') || '—' }}</span>
        <el-tag :type="item.result.routing_ok ? 'success' : 'danger'" size="small" class="tag">
          {{ item.result.routing_ok ? '✓' : '✗' }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="工具调用">
        <span class="muted">期望：{{ item.case.expected_tools.join('、') || '—' }}</span><br>
        <span>实际：{{ item.result.observation.tools.join('、') || '—' }}</span>
        <el-tag :type="item.result.tools_ok ? 'success' : 'danger'" size="small" class="tag">
          {{ item.result.tools_ok ? '✓' : '✗' }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="任务状态">
        <span class="muted">期望：{{ item.case.expected_status }}</span><br>
        <span>实际：{{ item.result.observation.status }}</span>
        <el-tag :type="item.result.status_ok ? 'success' : 'danger'" size="small" class="tag">
          {{ item.result.status_ok ? '✓' : '✗' }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="权限拒绝">
        {{ item.result.observation.permission_denied_tools.join('、') || '—' }}
        <el-tag :type="item.result.permission_ok ? 'success' : 'danger'" size="small" class="tag">
          {{ item.result.permission_ok ? '✓' : '✗' }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="证据来源">
        <span class="muted">期望：{{ item.case.expected_sources.join('、') || '—' }}</span><br>
        <span>实际：{{ item.result.observation.sources.join('、') || '—' }}</span>
        <el-tag
          v-if="item.result.grounded_ok !== null"
          :type="item.result.grounded_ok ? 'success' : 'danger'"
          size="small"
          class="tag"
        >
          {{ item.result.grounded_ok ? '✓' : '✗' }}
        </el-tag>
        <span v-else class="muted">（不适用）</span>
      </el-descriptions-item>
      <el-descriptions-item label="审批中断（HITL）">
        <span>实际：{{ item.result.observation.approval_required ? '是' : '否' }}</span>
        <el-tag
          v-if="item.result.hitl_ok !== null"
          :type="item.result.hitl_ok ? 'success' : 'danger'"
          size="small"
          class="tag"
        >
          {{ item.result.hitl_ok ? '✓' : '✗' }}
        </el-tag>
        <span v-else class="muted">（不适用）</span>
      </el-descriptions-item>
      <el-descriptions-item label="不匹配明细" :span="2">
        <template v-if="item.result.mismatches.length">
          <div v-for="(m, i) in item.result.mismatches" :key="i" class="mismatch">{{ m }}</div>
        </template>
        <span v-else class="pass">全部通过</span>
      </el-descriptions-item>
    </el-descriptions>
  </section>
</template>

<style scoped>
.case-detail h3 {
  margin: 0 0 8px;
  font-size: 1.05em;
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
  margin: 2px 0;
}
.tag {
  margin-left: 8px;
}
</style>
