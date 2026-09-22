/** axios 实例：withCredentials 携带 Cookie；access token 过期自动刷新并重试一次 */
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { tokenStorage } from './tokenStorage'

/** 后端结构化错误体：401/403 为 {"error": {...}}，409/422 为 {"detail": ...} */
interface ApiErrorBody {
  error?: { code?: string; message?: string }
  detail?: unknown
}

type RetryConfig = InternalAxiosRequestConfig & { _retry?: boolean }

export const client = axios.create({
  baseURL: '/api',
  timeout: 15000,
  // SSE 认证 Cookie 约定：跨域请求时浏览器自动携带 HttpOnly Cookie
  withCredentials: true,
})

client.interceptors.request.use((config) => {
  const access = tokenStorage.getAccess()
  if (access) config.headers.Authorization = `Bearer ${access}`
  return config
})

// ---------- 单飞刷新：并发 401 只触发一次 /auth/refresh ----------
let refreshing: Promise<string> | null = null

async function doRefresh(): Promise<string> {
  // refresh token 在 HttpOnly Cookie 中，浏览器自动携带；用裸 axios 避免走本实例的拦截器造成递归
  const resp = await axios.post('/api/auth/refresh', null, { withCredentials: true })
  const access = resp.data.access_token as string
  tokenStorage.save(access)
  return access
}

function refreshSession(): Promise<string> {
  refreshing ??= doRefresh().finally(() => {
    refreshing = null
  })
  return refreshing
}

/** 供 SSE 等非 axios 通道续期 Cookie：/auth/refresh 会同时轮转 SSE 与 Refresh 两个
 *  HttpOnly Cookie（EventSource 无法携带 Bearer 头）；失败返回 false，不触发跳登录 */
export async function refreshAuthCookie(): Promise<boolean> {
  try {
    await refreshSession()
    return true
  } catch {
    return false
  }
}

function redirectToLogin(): void {
  tokenStorage.clear()
  //硬跳转，彻底清空应用上下文
  window.location.assign('/login?expired=1')
}

client.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError) => {
    const config = error.config as RetryConfig | undefined
    const url = config?.url ?? ''
    const isAuthCall = url.includes('/auth/login') || url.includes('/auth/refresh')
    // 登录/刷新接口自身的 401 不重试；每个请求最多重试一次
    if (error.response?.status !== 401 || !config || config._retry || isAuthCall) throw error

    config._retry = true
    try {
      await refreshSession() // 新 access token 已入存储，重试时请求拦截器会自动携带
      return await client(config)
    } catch {
      redirectToLogin()
      throw error
    }
  },
)

/** 从后端错误响应中提取可展示的消息 */
export function extractErrorMessage(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const body = e.response?.data as ApiErrorBody | undefined
    if (body?.error?.message) return body.error.message
    if (typeof body?.detail === 'string') return body.detail
    if (Array.isArray(body?.detail)) {
      const first = body.detail[0] as { msg?: string } | undefined
      if (first?.msg) return first.msg
    }
    if (e.response) return `请求失败（HTTP ${e.response.status}）`
    return '网络异常，请稍后重试'
  }
  return e instanceof Error ? e.message : String(e)
}
