import { createI18n } from 'vue-i18n'

// 默认中英双语；vue-i18n 基础,可扩展更多语言。
// 演示外化 shell/导航/顶栏/开关;视图正文为可扩展部分。
const messages = {
  zh: {
    nav: { workbench: '工作台', leads: '线索管理', opps: '商机看板', playbook: '话术与政策', sop: 'SOP 规则', analytics: '运营统计', adminRoles: '角色与权限', adminRules: '渠道与分配' },
    app: { brand: '销售工作台', sub: '全渠道 CRM', search: '搜索客户 / 手机号 / 商机编号', me: '华东大客户组',
      theme: '主题', light: '亮色', dark: '暗色', auto: '随系统', lang: '语言' }
  },
  en: {
    nav: { workbench: 'Workbench', leads: 'Leads', opps: 'Opportunities', playbook: 'Playbook', sop: 'SOP Rules', analytics: 'Analytics', adminRoles: 'Roles & Perms', adminRules: 'Channels & Rules' },
    app: { brand: 'Sales Console', sub: 'Omni-CRM', search: 'Search customer / phone / opp id', me: 'East China KA Team',
      theme: 'Theme', light: 'Light', dark: 'Dark', auto: 'System', lang: 'Language' }
  }
}

export default createI18n({
  legacy: false,
  locale: localStorage.getItem('crm-lang') || 'zh',
  fallbackLocale: 'zh',
  messages
})
