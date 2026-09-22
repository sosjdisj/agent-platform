/** 认证接口：注册 / 登录 / 登出 / 当前用户（令牌持久化收敛在 tokenStorage） */
import { client } from './client'
import { tokenStorage } from './tokenStorage'

export interface User {
  id: number
  username: string
  email: string
  created_at: string
  permissions: string[]
}

interface TokenResponse {
  access_token: string
  token_type: string
}

export async function login(username: string, password: string): Promise<User> {
  const { data } = await client.post<TokenResponse>('/auth/login', { username, password })
  tokenStorage.save(data.access_token) // refresh token 由后端写入 HttpOnly Cookie
  return fetchMe()
}

export async function register(username: string, email: string, password: string): Promise<void> {
  await client.post('/auth/register', { username, email, password })
}

export async function fetchMe(): Promise<User> {
  const { data } = await client.get<User>('/auth/me')
  return data
}

export async function logout(): Promise<void> {
  try {
    await client.post('/auth/logout') // refresh token 在 Cookie 中，后端撤销并清除 Cookie
  } finally {
    tokenStorage.clear()
  }
}
