<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { extractErrorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

if (route.query.expired === '1') {
  ElMessage.warning('登录已失效，请重新登录')
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    await router.replace((route.query.redirect as string) || '/')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <el-card class="auth-card">
      <h2 class="title">Agent Platform 登录</h2>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="密码"
            autocomplete="current-password"
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="submit">
          登录
        </el-button>
      </el-form>
      <p class="hint">还没有账号？<router-link to="/register">去注册</router-link></p>
    </el-card>
  </main>
</template>

<style scoped>
.auth-page {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 10vh;
  min-height: 100vh;
  box-sizing: border-box;
}
.auth-card {
  width: 360px;
}
.title {
  margin: 0 0 16px;
  text-align: center;
}
.submit {
  width: 100%;
}
.hint {
  margin: 12px 0 0;
  text-align: center;
  color: #909399;
  font-size: 0.9em;
}
</style>
