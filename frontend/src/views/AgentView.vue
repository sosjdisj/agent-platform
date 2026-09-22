<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { createTask } from '@/api/tasks'
import { extractErrorMessage } from '@/api/client'
import PageHeader from '@/components/PageHeader.vue'

/** 任务输入页：提交分析任务，创建成功后回到任务列表（实时执行视图见 /analysis） */

const router = useRouter()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({
  title: '',
  query: '帮我分析客户 A 最近销售额下降的原因，如果需要可以查询客户资料、订单、销售数据和企业制度',
})

const rules: FormRules = {
  title: [
    { required: true, message: '请输入任务标题', trigger: 'blur' },
    { max: 200, message: '标题最多 200 个字符', trigger: 'blur' },
  ],
  query: [{ required: true, message: '请描述分析任务', trigger: 'blur' }],
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    await createTask(form.title.trim(), form.query.trim())
    ElMessage.success('任务已提交')
    await router.push('/tasks')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="container">
    <PageHeader title="新建分析任务" back-to="/tasks" back-label="返回任务列表" />

    <el-card shadow="never">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
        <el-form-item label="任务标题" prop="title">
          <el-input v-model="form.title" placeholder="例如：客户 A 销售额下降分析" maxlength="200" />
        </el-form-item>
        <el-form-item label="任务描述" prop="query">
          <el-input
            v-model="form.query"
            type="textarea"
            :rows="4"
            placeholder="描述要分析的问题，Agent 将按需调度数据查询与知识检索"
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="submitting">提交任务</el-button>
      </el-form>
      <p class="hint">提交后任务异步执行，可稍后在任务列表中查看状态与结果。</p>
    </el-card>
  </main>
</template>

<style scoped>
.container {
  max-width: 720px;
  margin: 32px auto;
  padding: 0 16px;
}
.hint {
  margin: 12px 0 0;
  color: #909399;
  font-size: 0.85em;
}
</style>
