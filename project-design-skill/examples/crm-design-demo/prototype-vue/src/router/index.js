import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/workbench' },
  { path: '/workbench', name: 'workbench', component: () => import('../views/Workbench.vue'), meta: { title: '工作台' } },
  { path: '/leads', name: 'leads', component: () => import('../views/Leads.vue'), meta: { title: '线索管理' } },
  { path: '/opportunities', name: 'opps', component: () => import('../views/Opportunities.vue'), meta: { title: '商机看板' } },
  { path: '/playbook', name: 'playbook', component: () => import('../views/Playbook.vue'), meta: { title: '话术与政策' } },
  { path: '/sop', name: 'sop', component: () => import('../views/Sop.vue'), meta: { title: 'SOP 规则' } },
  { path: '/analytics', name: 'analytics', component: () => import('../views/Analytics.vue'), meta: { title: '运营统计' } },
  { path: '/admin/roles', name: 'adminRoles', component: () => import('../views/AdminRoles.vue'), meta: { title: '角色与权限' } },
  { path: '/admin/rules', name: 'adminRules', component: () => import('../views/AdminRules.vue'), meta: { title: '渠道与分配' } }
]

export default createRouter({
  history: createWebHashHistory(),
  routes
})
