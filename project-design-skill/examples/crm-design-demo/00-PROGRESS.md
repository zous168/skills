# CRM 设计 — Progress（全闭环实施）

> **Set version:** v1.1（闭环完成 · 见 [CHANGELOG](CHANGELOG.md)/[README](README.md)）　·　输入基线 = 用户《CRM 需求规格说明书 v3》
> **全闭环实施中**：在 CRM 上跑完整 skill 流水线（Phase 0→6），按**任务切片串行交付**,
> 每篇文档走 **创建 → 审核 → 优化**、带版本头；子系统(坐席/后台/开放API)在同一套文档内合并覆盖(中等规模折叠)。
> 门禁:一篇全绿(optimized)→ 下一篇;只读探查可并行,交付串行。

**需求基线来源:** 用户提供的真实文档《CRM 需求规格说明书》(doc_version 3, 2026-06)。详细需求,Phase 0 视为已就绪。

**项目类型判定:** 产品型 B2B 内部系统(销售工作台 / 列表 / 漏斗看板)。→ 属 taste 声明的"非生成范围"(dashboard / 密集产品 UI)。

## 技术方向 & 子系统（Phase 0 决策 · 经 AskUserQuestion 选定）
| 维度 | 选择 |
|------|------|
| 前端 | Vue 3 + Element Plus + Vite + Pinia |
| 后端 | Node.js + NestJS |
| 数据库 | PostgreSQL（cfg/data/logs/stats 同库分 schema）|
| 缓存/队列 | Redis（BullMQ 队列 + 缓存）|
| 架构形态 | 模块化单体 |
| 子系统 | 坐席工作台(Web) · 管理后台(Admin) · 开放API(Hermes) |
| 响应式 | 桌面 ≥1025 / 平板 ≤1024 / 移动 ≤640 |

→ 原型已采用 **Element Plus 视觉语言**（#409EFF / 4px 圆角 / EP 标签与控件）；目录结构见 [`10-工程目录结构.md`](10-工程目录结构.md)。

## 本次原型范围
- **6 屏多页可点原型**(共享设计系统 `app.css` + 导航注入 `app.js`),覆盖需求规格主要模块:
  | 屏 | 文件 | 需求 |
  |---|------|------|
  | 工作台 | `prototype/index.html` | §4.6 + §4.7 概览 |
  | 线索管理 | `prototype/leads.html` | §4.1 录入/查重/分配/状态/来源 |
  | 商机看板 | `prototype/opportunities.html` | §4.2 阶段看板 + 赢单回写 + §4.3 |
  | 话术与政策库 | `prototype/playbook.html` | §4.4 差异化/版本/RAG/与 CSM 分域 |
  | SOP 规则引擎 | `prototype/sop.html` | §4.5 场景/节点/阈值/查询/灰度 |
  | 运营统计 | `prototype/analytics.html` | §4.7 漏斗/绩效/话术分析 |
- 生成器:**frontend-design** + **Element Plus 视觉语言**;taste 仅作审核镜。
- **交互已体现**(工作台):推进阶段→确认弹框、改派→抽屉表单、审批特价→权限门控表单弹框、关闭→危险确认,均带 Toast 操作反馈。

## 假设(需确认)
- A1: 工作台为销售坐席主视图,左侧待办队列 + 右侧商机详情双栏。基于 §4.6"待办线索/商机 + 客户全景只读聚合 + 人工操作"。
- A2: 客户全景档案、等级、CSM 售后摘要在本屏为**只读聚合**(§4.6),真实数据来自客户全景 / CSM 域。
- A3: 折扣/特价需经规则校验或人工审批(§4.2 权限控制),原型中"审批特价"为受控动作。

## Phase 1 — 全闭环切片计划与状态（01–14 + 汇编）
门禁串行;状态: pending → drafted → reviewed → optimized。已存在的文档先标 prior(待补版本头+一致性校验)。

| # | 文档(切片) | 文件 | 状态 |
|---|------|------|------|
| 01 | 需求规格(PRD,含角色×权限矩阵) | `01-需求规格.md` | **optimized**(本轮) |
| 02 | 领域分析 | `02-领域分析.md` | **optimized**(本轮) |
| 03 | 系统架构(含子系统分解) | `03-系统架构.md` | **optimized**(本轮) |
| 04 | 功能树(角色矩阵) | `04-功能树.md` | ✅ done(+版本头·一致性校验) |
| 05 | 数据设计(cfg/data/logs/stats) | `05-数据设计.md` | ✅ done(+版本头·映射 PG) |
| 06 | 接口设计(REST + OpenAPI/Swagger) | `06-接口设计.md` | ✅ done(+版本头·补 REST 章 v2) |
| 07 | 异步/后台处理(job/异步/调度) | `07-异步处理.md` | ✅ optimized |
| 08 | 非功能性需求(含 i18n/主题/响应式) | `08-非功能性需求.md` | ✅ optimized |
| 09 | 技术选型(含 DB 选型) | `09-技术选型.md` | ✅ optimized |
| 10 | 工程目录结构 | `10-工程目录结构.md`(已由 08 改名) | ✅ done(+版本头) |
| 11 | 风险评估 | `11-风险评估.md` | ✅ optimized |
| 12 | 测试策略 | `12-测试策略.md` | ✅ optimized |
| 13 | 路线图 | `13-路线图.md` | ✅ optimized |
| 14 | 任务计划与交付流程(切片+DoD) | `14-任务计划.md` | ✅ optimized |
| UX | 响应式/交互(断点) | `响应式设计.md` | ✅ done(+版本头) |
| PT | 原型(8 屏 + Vue 真框架 + i18n/主题) | `prototype-vue/` `prototype/` | ✅ done（闭环:补后台 2 页 + i18n + 主题,dist 实测）|
| 汇编 | README / CHANGELOG / consistency-report | 三文件 | ✅ done(Phase 5+6,Set v1.0) |

## Review trail（本轮全闭环）
- [01] 创建→审核→优化:见 doc 末「审核与优化」。发现并修:目标缺量化指标、2 个 FR 无验收→补;角色与 04 对齐。→ optimized
- [02] 创建→审核→优化:实体命名与 05 数据库对齐(lead/opportunity/follow_up);补状态机。→ optimized
- [03] 创建→审核→优化:子系统分解(坐席/后台/开放API)+ 部署视图;ADR 列关键决策;NFR 驱动追溯。→ optimized
- [07] 创建→审核→优化:11 条任务清单(异步/worker/调度)齐备触发/幂等/重试/失败/落库;调度均标 HA 防重跑;落库与 05 四库一致。→ optimized
- [Phase 0 补] 数据库门补问:**PostgreSQL(四 schema)+ Redis(BullMQ)**,记入技术方向;喂 05/09。
- [08/09/11/12/13/14] 逐篇创建→审核→优化:NFR 全量化(含 i18n/主题/响应式);选型含 DB 矩阵与取舍;风险登记 8 条;测试 FR/NFR 全追溯;路线图 M0–M4(MVP=M1);任务计划从功能树派生切片+DoD。→ 全 optimized
- [对齐] 08→10 改名;04/05/06/10/响应式 补版本头;06 补 REST 章(v2)。
- [Phase 5 一致性] 见 `consistency-report.md`:核心自洽;2 个 Major 为**原型落后于设计**(后台 B3/B4 页、i18n/主题)→ 记开口项/路线图,不阻塞定版。
- [Phase 6 汇编] README(索引+修订史)+ CHANGELOG + **Set version v1.0**。
- [原型编译预览] `vite.config.js` 加 `base:'./'`;`npm run build` → **`prototype-vue/dist/`**;**实测**纯静态服务器(python http.server,无 Vite)打开:布局满宽(shell 1280/aside 210/main 1070)、ElMessageBox 交互可用。`dist/` = 可双击/静态托管的交接预览。
- [闭环补完 · Set v1.1] 关闭 v1.0 两开口项 + 点选验收:
  - **后台两页**(角色×权限矩阵 / 渠道·分配规则)+ 导航/路由 → 缺口① 关闭(R32)。
  - **i18n 中英**(vue-i18n + el-config-provider)+ **主题 亮/暗/随系统**(EP 暗色)→ 缺口② 关闭(R23/R24)。
  - **dist 重构建 + 静态实测**:暗↔亮、中↔英两态截图;后台两页渲染(roles 28 开关 / rules 5 渠道);保存/推进→Toast(R33 点选验收,见 `prototype-notes.md` 清单)。
  - 一致性报告升 v2 → 无遗留开口项;README/CHANGELOG 升 **v1.1**。

## Review trail（原型阶段历史）

## Review trail
- [PT] taste 审核（见 `prototype-notes.md`）:
  - 通过:无 AI 默认味、单一强调色、语义点、tabular 数字签名、100dvh、可见文案零 em-dash。
  - 发现:[Major] emoji/Unicode 图标、缺空/载入态、未真正采用产品 DS;[Minor] 弱文本对比、未做移动端塌缩。
  - 真知:**taste §13 明确不生成密集产品 UI**;产品 UI 应以 Element Plus 等产品 DS 为底,taste 仅作反默认+可达性审核镜。
  - 已应用优化:① 图标 → 统一描边 SVG;② `--faint` 加深至 AA。
- [PT] 第 2 轮(回应用户反馈):
  - ③ 单屏 → **6 屏**覆盖各功能模块;
  - ④ 切换到**选定的 Element Plus 视觉语言**(原先是手写近似);
  - ⑤ 补齐**交互**:弹框/抽屉/Toast + 受控动作的权限门控(原 [Major] 缺交互/状态已闭合一部分);
  - ⑥ 新增 **08 工程目录结构**文档(框架驱动)。
- [PT] 第 3 轮(回应用户反馈,**已起静态服务实测验证**):
  - ⑦ 补齐**三态**:载入骨架 / 空态 / 错误态(leads 页状态切换器,实测 error 面板显示、data 隐藏);
  - ⑧ **移动端塌缩**:≤880px 左导航收为图标栏、多列堆叠(窄视口截图已验证);
  - ⑨ **交互联动**:线索行→详情抽屉、商机卡→工作台(`data-nav`);
  - ⑩ **大量操作接通**:新建/导入/分配/跟进/失效/发布版本/版本历史 等,经通用 `data-*` 引擎触发弹框/抽屉/Toast;
  - 实测:工作台“推进阶段→确认弹框→确认赢单→成功 Toast(已通知 CSM 与 Hermes)”闭环通过。
- [第 4 轮 · 6 项设计要素落地] 子系统确认(坐席/后台/开放API) + 重点四篇:
  - 功能树(含角色矩阵)/ 数据设计(cfg·data·logs·stats)/ 接口(清单+OpenAPI)/ 响应式(三档)。
  - 三档响应式**实测**:桌面 1280 展开导航双栏;平板 768 图标栏+工作台堆叠;移动 375 漏斗 KPI 换行(修复了溢出)。
- [第 5 轮 · 原型升级为真实框架] 从"长得像 EP 的静态 HTML" → **真实 Vue 3 + Element Plus 工程**(`prototype-vue/`):
  - **真实 el-* 组件**(el-menu/table/dialog/drawer/steps/timeline/affix…)、**页面+组件**拆分(views/ + components/ + router + mock)、**mock 数据**。
  - **实测**(起 Vite dev server):工作台渲染 ✓;ElMessageBox 确认框 ✓;ElMessage 反馈返回「已分配…已通知 Hermes」✓;路由切换/线索 el-table ✓。
  - 运行方式:`cd prototype-vue && npm install && npm run dev`(已装依赖,镜像 registry.npmmirror.com)。
  - 注:`prototype/`(静态 HTML)保留为早期"近似版";`prototype-vue/` 为当前真实框架原型。
