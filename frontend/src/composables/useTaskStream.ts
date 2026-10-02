/**
 * 任务执行流：SSE 实时订阅（HttpOnly Cookie 认证）+ 断链自动重开 + 轮询兜底 + 终态自动收尾。
 * 详情页与智能分析页共用，事件数据全部来自后端推送，页面不预置任何流程。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import {
  cancelTask,
  fetchTask,
  TASK_EVENT_LABELS,
  type AnalysisReport,
  type TaskResponse,
  type TraceEvent,
} from '@/api/tasks'
import { refreshAuthCookie } from '@/api/client'

const TERMINAL_EVENTS = new Set(['task_completed', 'task_failed', 'task_cancelled'])
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])
const POLL_INTERVAL_MS = 1500

export function useTaskStream(onEvent?: (event: TraceEvent) => void) {
  const task = ref<TaskResponse | null>(null)
  const events = ref<TraceEvent[]>([])
  /** 结构化九部分报告（任务完成落库后随详情返回） */
  const report = computed<AnalysisReport | null>(() => task.value?.report ?? null)

  const isTerminal = computed(() => !!task.value && TERMINAL_STATUSES.has(task.value.status))

  let source: EventSource | null = null
  let pollTimer: number | null = null
  let unmounted = false

  /** 实时帧订阅器（审批弹窗等跨组件消费），SSE 每帧逐一回调 */
  const listeners = new Set<(event: TraceEvent) => void>()

  /** 注册实时帧监听，返回退订函数（组件卸载时调用） */
  function onStreamEvent(listener: (event: TraceEvent) => void): () => void {
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }

  /** 停止订阅与轮询（到达终态 / 更换任务 / 页面卸载） */
  function dispose(): void {
    source?.close()
    source = null
    if (pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
  }

  /** 收到终态事件后拉取最终 result / error（update 内自动收尾订阅） */
  async function refresh(): Promise<void> {
    if (!task.value) return
    await update(await fetchTask(task.value.id))
  }

  function onFrame(e: MessageEvent): void {
    try {
      const event = JSON.parse(e.data as string) as TraceEvent
      events.value.push(event)
      onEvent?.(event)
      for (const listener of listeners) listener(event)
      if (TERMINAL_EVENTS.has(event.event)) void refresh().catch(() => {})
    } catch {
      // 非 JSON 帧降级为 message，仅保活，不进事件列表
    }
  }

  /** 建立 SSE 订阅 + 兜底轮询（update 与断链重开共用；已有订阅时安全跳过） */
  function connectEvents(): void {
    if (!task.value || source) return
    source = new EventSource(`/api/tasks/${task.value.id}/events`)
    source.addEventListener('message', onFrame)
    for (const name of Object.keys(TASK_EVENT_LABELS)) {
      source.addEventListener(name, onFrame)
    }
    // 连接被关闭（如 SSE Cookie 过期后重连被 401 拒绝）→ 轮转 Cookie 后重开；
    // readyState 为 CONNECTING 表示浏览器在自动重连，交给浏览器处理
    source.onerror = () => {
      if (source?.readyState !== EventSource.CLOSED) return
      dispose()
      void reopenEvents()
    }
    // 兜底轮询：订阅晚于任务启动会漏早期事件（Redis Pub/Sub 无回放），轮询保证状态推进可见
    pollTimer = window.setInterval(() => {
      void refresh().catch(() => {}) // 网络抖动忽略，下个周期重试
    }, POLL_INTERVAL_MS)
  }

  /** SSE 断链恢复：轻退避后轮转 Cookie（/auth/refresh 会同时续期 SSE Cookie），
   *  任务未终态时重开订阅；刷新失败（如已登出）则交给轮询兜底，不再重连 */
  async function reopenEvents(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 1000))
    if (unmounted) return
    if (!(await refreshAuthCookie())) return
    if (task.value && !isTerminal.value) connectEvents()
  }

  /** 应用任务最新状态：终态则收尾，非终态时确保 SSE + 兜底轮询在跑 */
  async function update(next: TaskResponse): Promise<void> {
    task.value = next
    if (TERMINAL_STATUSES.has(next.status)) {
      dispose()
      return
    }
    connectEvents()
  }

  /** 打开既有任务：拉取详情并自动订阅（终态则只渲染结果） */
  async function open(taskId: number): Promise<void> {
    reset()
    await update(await fetchTask(taskId))
  }

  /** 取消当前任务：状态推进到 cancelled 后自动收尾，已收到的事件轨迹保留 */
  async function cancel(): Promise<void> {
    if (!task.value) return
    await update(await cancelTask(task.value.id))
  }

  /** 清空状态并停止订阅（提交新任务前调用） */
  function reset(): void {
    dispose()
    task.value = null
    events.value = []
  }

  onBeforeUnmount(() => {
    unmounted = true
    dispose()
  })

  return { task, events, report, isTerminal, open, update, refresh, cancel, reset, onStreamEvent }
}
