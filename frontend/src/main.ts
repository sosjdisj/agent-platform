import { createApp } from 'vue'
import { createPinia } from 'pinia'
// 模板组件由 unplugin-vue-components 按需引入（见 vite.config.ts）；
// 函数式组件（代码中显式 import 的 ElMessage / ElMessageBox）与 v-loading 指令需手动引入样式 / 注册
import { ElLoadingDirective } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/loading/style/css'
// dev 下模板组件不注入按需样式（避免运行时发现新依赖 → 重新预构建 → 整页刷新），
// 改为启动时加载全量样式；prod 仍按需注入，此分支在构建时被 dead-code 消除
if (import.meta.env.DEV) await import('element-plus/dist/index.css')
import App from './App.vue'
import router from './router'

createApp(App)
  .use(createPinia())
  .use(router)
  .directive('loading', ElLoadingDirective)
  .mount('#app')
