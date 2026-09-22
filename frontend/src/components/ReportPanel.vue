<script setup lang="ts">
import type { AnalysisReport, ReportSource } from '@/api/tasks'

/** 分析报告展示（24.1）：九部分固定结构（六个文本段 + 证据 + 结论 + 建议）。
 *  知识证据标注来源文件名，并保留 document_id / chunk_id（悬停可见）供溯源验证 */
defineProps<{ report: AnalysisReport }>()

// 文本段渲染顺序：字段单一来源 = 后端 AnalysisReport
const TEXT_SECTIONS = [
  { key: 'customer_profile', title: '客户概况' },
  { key: 'sales_trend', title: '销售趋势' },
  { key: 'order_changes', title: '订单变化' },
  { key: 'product_changes', title: '产品变化' },
  { key: 'related_knowledge', title: '相关知识' },
  { key: 'possible_causes', title: '可能原因' },
] as const

/** 来源标签：knowledge 显示文件名 + chunk_id，tool 显示工具名(参数摘要) */
function sourceLabel(src: ReportSource): string {
  if (src.source_type === 'knowledge' && src.title) return `${src.title}（${src.ref_id}）`
  return src.ref_id
}

/** 来源悬停提示：完整溯源信息（source / document_id / chunk_id / 相关性分） */
function sourceTip(src: ReportSource): string {
  if (src.source_type !== 'knowledge') return `source=tool · ${src.ref_id}`
  const parts = [`source=knowledge`, `document_id=${src.document_id ?? '—'}`, `chunk_id=${src.ref_id}`]
  if (src.score != null) parts.push(`score=${src.score}`)
  return parts.join(' · ')
}
</script>

<template>
  <section v-for="s in TEXT_SECTIONS" :key="s.key" class="section">
    <template v-if="report[s.key]">
      <h3>{{ s.title }}</h3>
      <p class="section-body">{{ report[s.key] }}</p>
    </template>
  </section>

  <section v-if="report.evidence.length" class="section">
    <h3>证据</h3>
    <div v-for="(item, i) in report.evidence" :key="i" class="evidence">
      <p class="section-body">{{ item.content }}</p>
      <div class="sources">
        <span class="muted">来源：</span>
        <el-tag
          v-for="src in item.sources"
          :key="src.ref_id"
          size="small"
          :type="src.source_type === 'knowledge' ? 'primary' : 'info'"
          class="source-tag"
          :title="sourceTip(src)"
        >
          {{ sourceLabel(src) }}
        </el-tag>
      </div>
    </div>
  </section>

  <section v-if="report.conclusion" class="section">
    <h3>结论</h3>
    <p class="section-body">{{ report.conclusion }}</p>
  </section>

  <section v-if="report.suggestions.length" class="section">
    <h3>建议</h3>
    <ol class="suggestions">
      <li v-for="(item, i) in report.suggestions" :key="i">{{ item }}</li>
    </ol>
  </section>
</template>

<style scoped>
.section {
  margin-bottom: 16px;
}
.section h3 {
  margin: 0 0 6px;
  font-size: 1em;
  border-left: 3px solid #409eff;
  padding-left: 8px;
}
.section-body {
  margin: 0;
  white-space: pre-line;
  line-height: 1.7;
}
.evidence {
  margin-bottom: 12px;
}
.sources {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.source-tag {
  max-width: 100%;
  height: auto;
  overflow: hidden;
  text-overflow: ellipsis;
}
.suggestions {
  margin: 0;
  padding-left: 20px;
  line-height: 1.8;
}
.muted {
  color: #909399;
  font-size: 0.85em;
}
</style>
