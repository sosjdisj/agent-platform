/** 健康检查接口类型与请求封装 */

export interface ComponentStatus {
  status: 'up' | 'down'
  latency_ms?: number
  error?: string
}

export interface HealthReport {
  status: 'ok' | 'degraded'
  app: string
  version: string
  components: {
    api: ComponentStatus
    postgres: ComponentStatus
    redis: ComponentStatus
    qdrant: ComponentStatus
  }
}

export async function fetchHealth(): Promise<HealthReport> {
  const resp = await fetch('/health')
  if (!resp.ok) {
    throw new Error(`健康检查失败：HTTP ${resp.status}`)
  }
  return resp.json()
}
