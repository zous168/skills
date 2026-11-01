<template>
  <el-container class="shell">
    <el-aside :width="collapse ? '64px' : '210px'" class="aside">
      <div class="logo">
        <span class="mark">销</span>
        <span v-if="!collapse" class="logo-txt">{{ t('app.brand') }}<small>{{ t('app.sub') }}</small></span>
      </div>
      <el-menu :default-active="route.path" :collapse="collapse" router class="menu">
        <el-menu-item v-for="m in nav" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <template #title>{{ t('nav.' + m.key) }}</template>
        </el-menu-item>
      </el-menu>
      <div class="me">
        <el-avatar :size="28">张</el-avatar>
        <span v-if="!collapse" class="me-txt">张文<br><small class="faint">{{ t('app.me') }}</small></span>
      </div>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <strong class="title">{{ t('nav.' + (route.name || 'workbench')) }}</strong>
        <span class="sp" />
        <el-input v-model="kw" :placeholder="t('app.search')" :prefix-icon="Search" style="width: 240px" />
        <!-- 主题切换 -->
        <el-dropdown trigger="click" @command="c => themeMode = c">
          <el-button circle :icon="themeIcon" />
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="light" :icon="Sunny">{{ t('app.light') }}</el-dropdown-item>
              <el-dropdown-item command="dark" :icon="Moon">{{ t('app.dark') }}</el-dropdown-item>
              <el-dropdown-item command="auto" :icon="Monitor">{{ t('app.auto') }}</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <!-- 语言切换 -->
        <el-dropdown trigger="click" @command="setLang">
          <el-button>{{ locale === 'en' ? 'EN' : '中' }}</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="zh">中文</el-dropdown-item>
              <el-dropdown-item command="en">English</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main class="main"><router-view /></el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Search, HomeFilled, User, Aim, Notebook, Share, DataAnalysis, Lock, SetUp, Sunny, Moon, Monitor } from '@element-plus/icons-vue'
import { themeMode } from '../theme.js'

const route = useRoute()
const { t, locale } = useI18n()
const kw = ref('')
const collapse = ref(false)

const nav = [
  { path: '/workbench', key: 'workbench', icon: HomeFilled },
  { path: '/leads', key: 'leads', icon: User },
  { path: '/opportunities', key: 'opps', icon: Aim },
  { path: '/playbook', key: 'playbook', icon: Notebook },
  { path: '/sop', key: 'sop', icon: Share },
  { path: '/analytics', key: 'analytics', icon: DataAnalysis },
  { path: '/admin/roles', key: 'adminRoles', icon: Lock },
  { path: '/admin/rules', key: 'adminRules', icon: SetUp }
]

const themeIcon = computed(() => (themeMode.value === 'dark' ? Moon : themeMode.value === 'light' ? Sunny : Monitor))
function setLang (l) { locale.value = l; localStorage.setItem('crm-lang', l) }

const onResize = () => { collapse.value = window.innerWidth <= 1024 }
onMounted(() => { onResize(); window.addEventListener('resize', onResize) })
onUnmounted(() => window.removeEventListener('resize', onResize))
</script>

<style scoped>
.shell { height: 100vh; }
.aside { background: var(--el-bg-color); border-right: 1px solid var(--el-border-color-lighter); display: flex; flex-direction: column; transition: width .2s; }
.logo { display: flex; align-items: center; gap: 9px; padding: 14px 16px; font-weight: 650; }
.logo .mark { width: 26px; height: 26px; border-radius: 5px; background: var(--el-color-primary); color: #fff; display: grid; place-items: center; font-weight: 700; flex: 0 0 auto; }
.logo small { display: block; color: var(--el-text-color-placeholder); font-weight: 500; font-size: 10.5px; }
.menu { border-right: 0; flex: 1; }
.me { display: flex; align-items: center; gap: 9px; padding: 12px 16px; border-top: 1px solid var(--el-border-color-lighter); color: var(--el-text-color-regular); font-size: 12px; }
.topbar { display: flex; align-items: center; gap: 10px; background: var(--el-bg-color); border-bottom: 1px solid var(--el-border-color-lighter); }
.topbar .title { font-size: 15px; }
.main { background: var(--el-bg-color-page); padding: 0; }
</style>
