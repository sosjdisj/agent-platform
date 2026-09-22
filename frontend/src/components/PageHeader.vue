<script setup lang="ts">
import { useRouter } from 'vue-router'

/** 页面统一头部：返回按钮（可选）+ 标题/副标题 + 右侧操作区（默认插槽） */
defineProps<{
  title: string
  subtitle?: string
  /** 返回目标路由，如 /tasks；不传则不显示返回按钮 */
  backTo?: string
  backLabel?: string
}>()

const router = useRouter()
</script>

<template>
  <header class="page-header">
    <div class="title-wrap">
      <el-button v-if="backTo" text class="back-btn" @click="router.push(backTo)">
        ← {{ backLabel ?? '返回' }}
      </el-button>
      <div>
        <h1>{{ title }}</h1>
        <p v-if="subtitle" class="subtitle">{{ subtitle }}</p>
      </div>
    </div>
    <div class="actions">
      <slot />
    </div>
  </header>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.title-wrap {
  min-width: 0;
}
.back-btn {
  margin-left: -12px;
  color: #909399;
}
.back-btn:hover {
  color: var(--el-color-primary);
}
h1 {
  margin: 0;
  font-size: 1.4em;
}
.subtitle {
  margin: 4px 0 0;
  color: #909399;
  font-size: 0.9em;
}
.actions {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
}
</style>
