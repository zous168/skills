import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'   // 暗色主题
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import './theme.js'                                    // 应用 light/dark/auto
import './styles/theme.css'

const app = createApp(App)
for (const [key, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, comp)
}
app.use(router)
app.use(i18n)
app.use(ElementPlus)   // 语言由 App.vue 的 el-config-provider 动态提供
app.mount('#app')
