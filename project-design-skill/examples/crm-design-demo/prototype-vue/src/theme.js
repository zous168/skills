import { ref, watch } from 'vue'

// 主题:light / dark / auto(随系统)。默认 auto。
export const themeMode = ref(localStorage.getItem('crm-theme') || 'auto')
const mql = window.matchMedia('(prefers-color-scheme: dark)')

export function applyTheme () {
  const dark = themeMode.value === 'dark' || (themeMode.value === 'auto' && mql.matches)
  document.documentElement.classList.toggle('dark', dark)
}
mql.addEventListener('change', () => { if (themeMode.value === 'auto') applyTheme() })
watch(themeMode, v => { localStorage.setItem('crm-theme', v); applyTheme() }, { immediate: true })
