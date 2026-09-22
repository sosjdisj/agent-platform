<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { extractErrorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', email: '', password: '', confirm: '' })

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '长度 3-50 个字符', trigger: 'blur' },
    { pattern: /^[a-zA-Z0-9_]+$/, message: '仅限字母、数字与下划线', trigger: 'blur' },
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '至少 8 个字符', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) =>
        value === form.password ? callback() : callback(new Error('两次输入的密码不一致')),
      trigger: 'blur',
    },
  ],
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.register(form.username, form.email, form.password)
    // 注册成功后自动登录
    await auth.login(form.username, form.password)
    await router.replace('/')
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
      <h2 class="title">注册账号</h2>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="3-50 位字母、数字或下划线" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="邮箱" autocomplete="email" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="至少 8 个字符"
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            show-password
            placeholder="再次输入密码"
            autocomplete="new-password"
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="submit">
          注册并登录
        </el-button>
      </el-form>
      <p class="hint">已有账号？<router-link to="/login">去登录</router-link></p>
    </el-card>
  </main>
</template>

<style scoped>
.auth-page {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 6vh;
  min-height: 100vh;
  box-sizing: border-box;
}
.auth-card {
  width: 400px;
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
