/** 认证状态：当前用户与登录态（令牌持久化属于 api 层，见 tokenStorage） */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchMe, login as apiLogin, logout as apiLogout, register as apiRegister } from '@/api/auth'
import type { User } from '@/api/auth'
import { tokenStorage } from '@/api/tokenStorage'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const initialized = ref(false)

  const isAuthenticated = computed(() => user.value !== null)

  /** 应用启动后首次导航时调用：从本地令牌恢复登录态（刷新页面后保持登录） */
  async function init(): Promise<void> {
    if (initialized.value) return
    initialized.value = true
    if (!tokenStorage.getAccess()) return
    try {
      user.value = await fetchMe()
    } catch {
      // 401 时拦截器已尝试刷新并重试；刷新失败会跳转登录页，此处无需处理
    }
  }

  async function login(username: string, password: string): Promise<void> {
    user.value = await apiLogin(username, password)
  }

  async function register(username: string, email: string, password: string): Promise<void> {
    await apiRegister(username, email, password)
  }

  async function logout(): Promise<void> {
    try {
      await apiLogout()
    } finally {
      user.value = null
    }
  }

  return { user, initialized, isAuthenticated, init, login, register, logout }
})
