<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

/** 全局壳：登录后页面套顶部导航栏，公开页（登录/注册）独立渲染 */

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const isPublic = computed(() => route.meta.public === true)

/** 高亮一级菜单：/tasks/3 → /tasks */
const activeMenu = computed(() => (route.path === '/' ? '/' : `/${route.path.split('/')[1]}`))

async function onLogout() {
  await auth.logout()
  ElMessage.success('已退出登录')
  await router.replace('/login')
}
</script>

<template>
  <div v-if="!isPublic" class="app-shell">
    <header class="app-header">
      <div class="inner">
        <a class="brand" @click="router.push('/')">Agent Platform</a>
        <el-menu
          mode="horizontal"
          :default-active="activeMenu"
          :ellipsis="false"
          router
          class="nav-menu"
        >
          <el-menu-item index="/">首页</el-menu-item>
          <el-menu-item index="/agent">新建任务</el-menu-item>
          <el-menu-item index="/tasks">任务列表</el-menu-item>
          <el-menu-item index="/approvals">审批中心</el-menu-item>
          <el-menu-item index="/analysis">智能分析</el-menu-item>
          <el-menu-item index="/evaluation">评估 Dashboard</el-menu-item>
        </el-menu>
        <div v-if="auth.user" class="user">
          <span class="username">{{ auth.user.username }}</span>
          <el-button size="small" text @click="onLogout">退出登录</el-button>
        </div>
      </div>
    </header>

    <router-view v-slot="{ Component }">
      <transition name="page" mode="out-in">
        <component :is="Component" />
      </transition>
    </router-view>
  </div>

  <router-view v-else />
</template>

<style>
/* ---- 主题：中性灰白底 + 单一主色，克制用色 ---- */
:root {
  --el-color-primary: #1677ff;
  --el-color-primary-light-3: #4c9bff;
  --el-color-primary-light-5: #7cb8ff;
  --el-color-primary-light-7: #aed4ff;
  --el-color-primary-light-8: #c6e0ff;
  --el-color-primary-light-9: #e8f1ff;
  --el-color-primary-dark-2: #0e5fd8;
}

body {
  margin: 0;
  font-family: system-ui, sans-serif;
  background: #f6f7f9;
  color: #1f2329;
}

/* ---- 全局导航栏 ---- */
.app-header {
  position: sticky;
  top: 0;
  z-index: 100;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
}
.app-header .inner {
  display: flex;
  align-items: center;
  gap: 24px;
  max-width: 1200px;
  height: 56px;
  margin: 0 auto;
  padding: 0 24px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 1.05em;
  font-weight: 700;
  color: #1f2329;
  white-space: nowrap;
  cursor: pointer;
}
.brand::before {
  content: '';
  width: 10px;
  height: 10px;
  border-radius: 3px;
  background: var(--el-color-primary);
}
.nav-menu.el-menu--horizontal {
  flex: 1;
  --el-menu-horizontal-height: 56px;
  --el-menu-active-color: var(--el-color-primary);
  border-bottom: none;
}
.user {
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.username {
  color: #606266;
  font-size: 0.9em;
}

/* ---- 页面切换过渡 ---- */
.page-enter-active,
.page-leave-active {
  transition: opacity 0.15s ease;
}
.page-enter-from,
.page-leave-to {
  opacity: 0;
}
</style>
