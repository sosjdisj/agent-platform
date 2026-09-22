/** 令牌持久化：仅保存短效 access token；长效 refresh token 走 HttpOnly Cookie，JS 不可读 */
const ACCESS_KEY = 'agent.access_token'

export const tokenStorage = {
  getAccess(): string | null {
    return localStorage.getItem(ACCESS_KEY)
  },
  save(accessToken: string): void {
    localStorage.setItem(ACCESS_KEY, accessToken)
  },
  clear(): void {
    localStorage.removeItem(ACCESS_KEY)
  },
}
