/** 路由：/login、/register 公开，其余需登录（守卫内完成登录态恢复） */
import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** 公开页面（无需登录） */
    public?: boolean
  }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/RegisterView.vue'),
      meta: { public: true },
    },
    { path: '/', name: 'home', component: () => import('@/views/HomeView.vue') },
    { path: '/agent', name: 'agent', component: () => import('@/views/AgentView.vue') },
    { path: '/tasks', name: 'tasks', component: () => import('@/views/TasksView.vue') },
    { path: '/tasks/:id', name: 'task-detail', component: () => import('@/views/TaskDetailView.vue') },
    { path: '/approvals', name: 'approvals', component: () => import('@/views/ApprovalsView.vue') },
    { path: '/analysis', name: 'analysis', component: () => import('@/views/AnalysisView.vue') },
    { path: '/evaluation', name: 'evaluation', component: () => import('@/views/EvaluationView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.init()
  if (!to.meta.public && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  // 已登录用户访问登录/注册页时回到首页
  if (to.meta.public && auth.isAuthenticated) {
    return { name: 'home' }
  }
})

export default router
