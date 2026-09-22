<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchHealth, type HealthReport } from '@/api/health'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const report = ref<HealthReport | null>(null)
const healthError = ref<string | null>(null)

async function refreshHealth() {
  healthError.value = null
  try {
    report.value = await fetchHealth()
  } catch (e) {
    healthError.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(refreshHealth)
</script>

<template>
  <main class="container">
    <section class="hero">
      <h1>客户智能分析平台</h1>
      <p>
        提交分析任务，Agent 按需调度数据查询与知识检索，生成结构化分析报告；
        支持高危操作人工审批与全链路效果评估。
      </p>
      <div class="hero-actions">
        <el-button type="primary" size="large" @click="router.push('/agent')">
          新建分析任务
        </el-button>
        <el-button size="large" @click="router.push('/analysis')">打开智能分析</el-button>
      </div>
    </section>

    <section class="grid">
      <div class="entry" @click="router.push('/agent')">
        <h3>新建分析任务</h3>
        <p>描述要分析的问题，任务异步执行并实时推送事件。</p>
        <span class="go">前往 →</span>
      </div>
      <div class="entry" @click="router.push('/tasks')">
        <h3>任务列表</h3>
        <p>查看任务状态、执行详情与分析报告。</p>
        <span class="go">前往 →</span>
      </div>
      <div class="entry" @click="router.push('/approvals')">
        <h3>审批中心</h3>
        <p>处理高危操作的人工审批，批准或拒绝。</p>
        <span class="go">前往 →</span>
      </div>
      <div class="entry" @click="router.push('/analysis')">
        <h3>智能分析</h3>
        <p>实时观看 Multi-Agent 执行事件流与报告生成。</p>
        <span class="go">前往 →</span>
      </div>
      <div class="entry" @click="router.push('/evaluation')">
        <h3>评估 Dashboard</h3>
        <p>七项指标、错误案例清单与单案例详情。</p>
        <span class="go">前往 →</span>
      </div>
    </section>

    <section class="bottom">
      <el-card shadow="never">
        <template #header>当前用户</template>
        <template v-if="auth.user">
          <p class="no-margin">
            <strong>{{ auth.user.username }}</strong>
            <span class="muted">（ID: {{ auth.user.id }}，{{ auth.user.email }}）</span>
          </p>
          <p class="no-margin">
            权限：
            <template v-if="auth.user.permissions.length">
              <el-tag v-for="p in auth.user.permissions" :key="p" size="small" class="tag">
                {{ p }}
              </el-tag>
            </template>
            <span v-else class="muted">暂无权限</span>
          </p>
        </template>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="card-header">
            <span>后端健康检查</span>
            <el-button size="small" @click="refreshHealth">刷新状态</el-button>
          </div>
        </template>
        <p v-if="healthError" class="error no-margin">{{ healthError }}（请确认后端已在 8000 端口启动）</p>
        <template v-else-if="report">
          <p class="no-margin">
            整体状态：
            <strong :class="report.status">{{ report.status }}</strong>
            （v{{ report.version }}）
          </p>
          <ul>
            <li v-for="(item, name) in report.components" :key="name">
              <strong>{{ name }}</strong>: {{ item.status }}
              <span v-if="item.latency_ms != null">（{{ item.latency_ms }} ms）</span>
              <span v-if="item.error" class="error">（{{ item.error }}）</span>
            </li>
          </ul>
        </template>
        <p v-else class="no-margin">加载中...</p>
      </el-card>
    </section>
  </main>
</template>

<style scoped>
.container {
  max-width: 1100px;
  margin: 24px auto;
  padding: 0 16px;
}

/* ---- 头部 ---- */
.hero {
  padding: 28px 32px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
}
.hero h1 {
  margin: 0;
  font-size: 1.5em;
  color: #1f2329;
}
.hero p {
  max-width: 640px;
  margin: 10px 0 0;
  color: #8a919f;
  line-height: 1.6;
}
.hero-actions {
  display: flex;
  gap: 12px;
  margin-top: 20px;
}

/* ---- 功能宫格 ---- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
  margin: 20px 0;
}
.entry {
  padding: 18px 20px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
}
.entry:hover {
  border-color: var(--el-color-primary-light-7);
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.06);
  transform: translateY(-1px);
}
.entry h3 {
  margin: 0;
  font-size: 1em;
  font-weight: 600;
  color: #1f2329;
}
.entry p {
  min-height: 42px;
  margin: 8px 0 0;
  color: #8a919f;
  font-size: 0.85em;
  line-height: 1.5;
}
.go {
  color: var(--el-color-primary);
  font-size: 0.85em;
}

/* ---- 底部两栏 ---- */
.bottom {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.no-margin {
  margin: 0 0 8px;
}
.tag {
  margin-right: 4px;
}
.muted {
  color: #909399;
  font-size: 0.9em;
}
.ok {
  color: #16a34a;
}
.degraded {
  color: #dc2626;
}
.error {
  color: #dc2626;
  font-size: 0.9em;
}
ul {
  list-style: none;
  padding: 0;
  margin: 0;
}
li {
  padding: 4px 0;
  border-bottom: 1px solid #eee;
}
</style>
