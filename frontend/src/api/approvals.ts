/** 审批中心 API：列表 + 决定（请求来自后端真实 HIGH 工具 interrupt，契约见 18.4 事件） */
import { client } from './client'

export interface ApprovalItem {
  id: number
  task_id: number
  task_title: string | null
  tool: string
  risk_level: string | null
  parameter_summary: string | null
  requester_id: number
  status: string
  decision: string | null
  comment: string | null
  created_at: string
  decided_at: string | null
}

export type ApprovalDecision = 'approved' | 'rejected'

/** 风险等级展示映射（值域 = 后端 RiskLevel，小写） */
export const RISK_LABELS: Record<string, string> = {
  high: '高危',
  medium: '中危',
  low: '低危',
}

export const RISK_TYPES: Record<string, 'danger' | 'warning' | 'info'> = {
  high: 'danger',
  medium: 'warning',
  low: 'info',
}

/** 审批记录状态展示映射（pending / approved / rejected） */
export const APPROVAL_STATUS_LABELS: Record<string, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已拒绝',
}

export const APPROVAL_STATUS_TYPES: Record<string, 'warning' | 'success' | 'danger'> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'danger',
}

export async function fetchApprovals(): Promise<ApprovalItem[]> {
  const { data } = await client.get<ApprovalItem[]>('/approvals')
  return data
}

export async function decideApproval(
  id: number,
  decision: ApprovalDecision,
  comment?: string,
): Promise<ApprovalItem> {
  const { data } = await client.post<ApprovalItem>(`/approvals/${id}/decide`, {
    decision,
    comment: comment || null,
  })
  return data
}
