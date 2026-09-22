/** 评估报告 API（24.2 Dashboard 数据源）：结构契约 = 后端 EvaluationReport（Prompt 22.3） */
import { client } from './client'

/** 单指标值：比率类 value ∈ [0, 1]（average_latency_ms 为毫秒均值），n 为分母，null = 不适用 */
export interface MetricValue {
  value: number | null
  n: number
}

/** 总体指标：与后端 MetricsReport 字段一一对应 */
export interface MetricsReport {
  total_cases: number
  passed_cases: number
  agent_routing_accuracy: MetricValue
  tool_selection_accuracy: MetricValue
  task_completion_rate: MetricValue
  rag_groundedness: MetricValue
  permission_violation_rate: MetricValue
  hitl_correctness: MetricValue
  average_latency_ms: MetricValue
}

/** 评估案例声明（期望面） */
export interface EvaluationCase {
  id: string
  name: string
  input: string
  user_role: string
  expected_agents: string[]
  expected_tools: string[]
  expected_outcome: string
  expected_status: string
  requires_approval: boolean
  expected_sources: string[]
}

/** 单案例实际行为采集（轨迹 + 任务终态） */
export interface CaseObservation {
  agents: string[]
  tools: string[]
  permission_denied_tools: string[]
  approval_required: boolean
  status: string
  sources: string[]
}

/** 单案例判定结果：各维度对错（null = 不适用）+ 不匹配明细 */
export interface CaseRunResult {
  case_id: string
  observation: CaseObservation
  latency_ms: number
  completed: boolean
  routing_ok: boolean
  tools_ok: boolean
  status_ok: boolean
  permission_ok: boolean
  grounded_ok: boolean | null
  hitl_ok: boolean | null
  mismatches: string[]
}

/** 单案例详情：案例声明 × 判定结果 */
export interface CaseDetail {
  case: EvaluationCase
  result: CaseRunResult
}

/** 评估报告聚合：failures 由后端从 cases 推导（错误案例清单） */
export interface EvaluationReport {
  generated_at: string
  metrics: MetricsReport
  cases: CaseDetail[]
  failures: CaseDetail[]
}

export async function fetchEvaluationReport(): Promise<EvaluationReport> {
  const resp = await client.get<EvaluationReport>('/evaluation/report')
  return resp.data
}
