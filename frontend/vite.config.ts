import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  plugins: [
    vue(),
    // Element Plus 模板组件按需引入（组件 + 样式），替代 main.ts 全量注册，大幅减小首屏体积。
    // dev 下关闭样式注入：全量 CSS 改由 main.ts 静态导入（扫描器可见，启动即预构建），
    // 否则各页面首次访问会"运行时发现"样式子路径，触发依赖重新预构建 + 整页刷新
    Components({
      resolvers: [ElementPlusResolver({ importStyle: mode === 'development' ? false : 'css' })],
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 开发环境下将后端接口代理到 FastAPI 服务
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/health': { target: 'http://localhost:8000', changeOrigin: true },
    },
    // 启动时预热转换全部页面/组件，首次导航无需等待按需编译
    warmup: {
      clientFiles: ['./src/main.ts', './src/App.vue', './src/**/*.vue'],
    },
  },
  optimizeDeps: {
    // resolver 注入的组件导入（element-plus/es）是 SFC 转换后才出现的裸依赖，
    // 启动扫描不可见，须显式声明。dev 下样式不再按需注入（走 main.ts 的全量
    // CSS），无需列举各组件 style 子路径；main.ts 的 style 导入在静态链上，
    // 扫描器可见，无需声明。
    include: ['element-plus/es'],
  },
  build: {
    chunkSizeWarningLimit: 900,
    rolldownOptions: {
      output: {
        // 稳定依赖独立分包：业务代码迭代不影响其浏览器缓存
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (id.includes('element-plus')) return 'element-plus'
          if (id.includes('/axios/')) return 'axios'
          if (
            id.includes('/vue/') ||
            id.includes('/@vue/') ||
            id.includes('vue-router') ||
            id.includes('/pinia/') ||
            id.includes('vue-demi')
          ) {
            return 'vue-vendor'
          }
        },
        // oxc 压缩：移除 console（debugger 语句默认移除）
        minify: { compress: { dropConsole: true }, mangle: true },
      },
    },
  },
}))
