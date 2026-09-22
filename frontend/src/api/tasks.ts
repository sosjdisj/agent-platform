/** 任务 API：创建 / 详情 / 取消 + 结构化报告类型（24.1 报告页数据源） */
import { client } from './client'

export interface TaskResponse {
  id: number
  user_id: number
  title: string
  query: string
  result: string | null
  /** 结构化九部分报告（结构 = 后端 AnalysisReport），未完成 / 历史行为 null */
  report: AnalysisReport | null
  error: string | null
  status: string
  finished_at: string | null
  created_at: string
}

/** 任务状态展示映射（status 值域 = 后端 AgentStatus 枚举，小写） */
export const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '待执行',
  running: '执行中',
  waiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export const TASK_STATUS_TYPES: Record<string, 'info' | 'warning' | 'success' | 'danger'> = {
  pending: 'info',
  running: 'warning',
  waiting_approval: 'warning',
  completed: 'success',
  failed: 'danger',
  cancelled: 'info',
}

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status
}

/** SSE 事件展示文案（值域 = 后端 AgentTraceEventType 枚举；事件名清单即此映射的键） */
export const TASK_EVENT_LABELS: Record<string, string> = {
  task_started: '任务开始',
  agent_selected: '选择 Agent',
  agent_finished: 'Agent 完成',
  llm_call: 'LLM 调用',
  tool_called: '调用工具',
  tool_result: '工具返回',
  permission_denied: '权限拒绝',
  approval_required: '等待审批',
  approval_result: '审批结果',
  task_completed: '任务完成',
  task_failed: '任务失败',
  task_cancelled: '任务取消',
}

/** SSE 轨迹事件（扁平 JSON + event 字段，契约见后端 app/schemas/events.py） */
export interface TraceEvent {
  event: string
  task_id: number
  id?: number // 轨迹回放（REST）条目带落库 id；SSE 实时帧没有
  timestamp?: string // 轨迹回放条目的服务器时间；SSE 实时帧没有
  agent?: string | null
  tool?: string | null
  round?: number | null
  duration_ms?: number | null
  status?: string | null
  parameter_summary?: string | null
  result_summary?: string | null
  error_code?: string | null
}

/** 报告证据来源（结构 = 后端 agents.state.Source）：knowledge=知识库分块
 *  （title=文档文件名、document_id=文档 ID、ref_id=chunk_id、score=相关性分），
 *  tool=工具输出记录（ref_id=工具名(参数摘要)） */
export interface ReportSource {
  source_type: 'knowledge' | 'tool'
  ref_id: string
  title: string | null
  score: number | null
  document_id: string | null
}

/** 报告证据：一条结论依据 + 支撑它的来源（可溯源） */
export interface ReportEvidence {
  content: string
  sources: ReportSource[]
}

/** 结构化分析报告（结构 = 后端 AnalysisReport，九部分报告的单一来源） */
export interface AnalysisReport {
  customer_profile: string
  sales_trend: string
  order_changes: string
  product_changes: string
  related_knowledge: string
  possible_causes: string
  evidence: ReportEvidence[]
  conclusion: string
  suggestions: string[]
}

export async function createTask(title: string, query: string): Promise<TaskResponse> {
  const resp = await client.post<TaskResponse>('/tasks', { title, query })
  return resp.data
}

export async function fetchTasks(offset = 0, limit = 50): Promise<TaskResponse[]> {
  const resp = await client.get<TaskResponse[]>('/tasks', { params: { offset, limit } })
  return resp.data
}

/** 轨迹回放（23.4 时间线数据源）：keyset 分页，最新在前；与 SSE 流同源于 agent_trace_events */
export async function fetchTracePage(
  taskId: number,
  beforeId?: number,
  limit = 50,
): Promise<TraceEvent[]> {
  const resp = await client.get<TraceEvent[]>(`/tasks/${taskId}/trace`, {
    params: { before_id: beforeId, limit },
  })
  return resp.data
}

export async function fetchTask(id: number): Promise<TaskResponse> {
  const resp = await client.get<TaskResponse>(`/tasks/${id}`)
  return resp.data
}

export async function cancelTask(id: number): Promise<TaskResponse> {
  const resp = await client.post<TaskResponse>(`/tasks/${id}/cancel`)
  return resp.data
}
