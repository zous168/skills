# CHANGELOG — CRM 设计集

## v1.1 — 2026-06-19
- **闭环补完**(关闭 v1.0 的 2 个开口项,`prototype-vue`):
  - 补管理后台两页:**角色与权限**(角色×权限矩阵)、**渠道与分配规则** + 导航/路由 →(缺口①)。
  - 实装 **i18n 中英**(vue-i18n + `el-config-provider`)+ **主题 亮/暗/随系统**(EP 暗色)→(缺口②)。
- **交互点选验收**:编译 `dist/` 静态实测——推进阶段/保存→Toast、主题/语言切换、后台两页渲染(详见 `prototype-notes.md` 验收清单)。
- 一致性报告升 v2:无遗留开口项(原型 = 设计)。

## v1.0 — 2026-06-19
- 首个**完整设计集**,由 `project-design-docs` skill 全闭环产出(Phase 0→6)。
- 文档 01–14 全部 `optimized`(各带版本头 `doc_version`),含:需求/领域/架构/功能树/数据(cfg·data·logs·stats)/接口(REST+OpenAPI)/异步/NFR/技术选型/工程结构/风险/测试/路线图/任务计划。
- 技术方向(Phase 0):Vue 3 + Element Plus / NestJS / **PostgreSQL + Redis(BullMQ)** / 模块化单体;三子系统(坐席/后台/开放API);三档响应式;中英 i18n + 亮/暗/随系统(默认)。
- 原型:`prototype-vue/`(Vue 3 + EP 真框架,6 屏,交互+三态+响应式,实测通过)。
- 一致性审核通过;**开口项**:原型未覆盖后台 B3/B4、原型未实装 i18n/主题(设计已要求,记入路线图/落地)。
