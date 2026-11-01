# 原型 taste 审核（Phase 4b）

**对象:** `prototype/index.html`（销售人工工作台单屏）
**审核依据:** taste skill `design-taste-frontend` §14 FINAL PRE-FLIGHT CHECK + §9 AI Tells + §13 OUT OF SCOPE。
**重要前提:** 本屏是密集产品 UI / admin 面板 —— 落在 taste **§13 明确的"非生成范围"**。因此:
- taste 的判定:此类 UI 不该用 taste 生成,应采用 **Carbon / Fluent / Polaris** 产品设计体系 + TanStack/AG Grid 处理表格。
- §14 中 landing 专用项（hero / eyebrow / zigzag / logo wall / marquee / scroll cue / 营销文案 em-dash / premium 配色 / serif 纪律 / bento）**整体 N/A**。
- 适用的是 **可迁移子集**:反 AI 默认、单一强调色、一致圆角、对比度、图标规范、信息密度、空/载入/错误态、视口稳定。

## 通过项（pass）
- ✅ **无 AI 默认味**:字体走系统栈（PingFang/Segoe），**非 Inter 默认**;强调色 `#34408B` 靛蓝,**非 AI 紫渐变**;无玻璃拟态;无"三等分卡片";公司/人名为真实化中文（非 Acme/Jane Doe）。
- ✅ **单一强调色锁**:`#34408B` 在选中/阶段/主按钮一致使用;语义色（绿=赢单、琥珀=逾期、红=高优/风险）仅用于真实状态。
- ✅ **语义点而非装饰点**:优先级圆点对应真实优先级,非装饰。
- ✅ **长列表只用 `border-bottom`**（未给每行加 top+bottom 双边框）。
- ✅ **类型签名成立**:金额/ID/traceId/时间统一 `tabular-nums` 等宽,数据工具气质明确。
- ✅ **视口稳定**:用 `100dvh`,未用 `h-screen`;无炫技动画,无需 reduced-motion 包裹。
- ✅ **信息密度合理**:队列 6 行、无 20 行巨表;子段落短。
- ✅ **可见文案零 em-dash**（统一用 `·` / 全角括号）。

## 发现（findings）
- **[Major] 图标是 Unicode/emoji 与文字符号**（`⌕` 搜索、`✦` AI、`🔒` 锁、`▲▼` 趋势、`✓` 勾）。taste §14 要求图标来自 **Phosphor / Tabler / Radix** 等库,禁止手搓/emoji —— `🔒` 在不同系统会渲染成彩色 emoji,破坏统一性。→ 换成统一描边 SVG 图标。
- **[Major] 缺空/载入/错误态**。taste §14 与本 skill Rubric D 都要求。当前仅有"有数据"态:线索/商机为空、加载中、接口失败均无表达。→ 至少补线索 tab 空态 + 队列骨架屏。
- **[Major / 建议] 未真正采用产品设计体系**。现为"近 Carbon/Fluent"的手写近似。taste §13 对密集产品 UI 的明确指引是直接落到 **Carbon 或 Fluent** 的 token/组件上。→ 生产实现时以 Carbon `@carbon/react` 或 Fluent v9 为基,而非纯手写 CSS。
- **[Minor] 弱文本对比偏低**。`--faint:#8A93A2` 用于 `.wait` / `.cid` 等元信息,在近白底上约 2.9:1,小字未达 WCAG AA(4.5:1)。占位符可放宽,但元信息应加深至约 `#6B7480`。
- **[Minor] 未做移动端塌缩**。固定 `362px 1fr` 双栏 + `overflow:hidden`,窄屏会破版。对"桌面端内部工作台"可接受,但应作为**显式范围决定**记录,而非默认遗漏。
- **[Minor] “审批特价”按钮对比**:`#9A6206` 文字 / `#FBF1DD` 底约 4.8:1,勉强达标;换 SVG 锁图标后复核。

## 结论
单屏作为**设计意图验证**是站得住的:布局、字段、阶段流、跟进时间线、客户全景只读聚合都忠实于需求规格,且避开了主要 AI 设计味。**但它印证了一个真知**:taste 不是产品型 UI 的生成器,产品 UI 应以 Carbon/Fluent 为底、taste 只作"反默认 + 可达性"审核镜。

## 本次应用的优化（4c）
1. ✅ 全部 Unicode/emoji 图标（`⌕ ✦ 🔒 ▲▼ ✓`）→ 统一描边 inline SVG（修 [Major] 图标）。
2. ✅ `--faint` 加深 `#8A93A2 → #6B7480`,元信息小字达 WCAG AA（修 [Minor] 对比）。

## 回灌设计文档（留待完整跑）
- [Major] 空 / 载入 / 错误态 → 进 UX 交互文档的"状态设计"小节。
- [Major/建议] 采用 Carbon `@carbon/react` 或 Fluent v9 作为产品 DS → 进 03-系统架构 / 07-技术选型。
- [Minor] 移动端策略：明确"桌面端内部工作台,不做窄屏塌缩"作为显式范围决定 → 进 06-非功能性需求 / UX 文档。

---

## 闭环补完（Set v1.1 · 真实框架 `prototype-vue/`）
> 关闭一致性报告的两个开口项 + 交互点选验收(R32/R33/R23/R24)。

**子系统覆盖(R32):** 补出管理后台两页 —— `AdminRoles.vue`(角色×权限矩阵,7 能力×4 角色)、`AdminRules.vue`(渠道启用/默认分配组 + 分配策略/并发/查重/升级)+ 导航 + 路由。坐席工作台 + 后台 SOP/话术/统计 + 后台角色/规则均覆盖;开放 API 无界面(由 06 接口覆盖)。→ **缺口① 关闭**。
**i18n + 主题(R23/R24):** vue-i18n 中英 + `el-config-provider` 动态 locale;EP 暗色 css-vars + `theme.js`(亮/暗/随系统);顶栏主题/语言切换。→ **缺口② 关闭**。

### 交互验收清单(R33 · 编译 `dist/` 静态实测)
| 元素 | 预期 | 验证 |
|------|------|------|
| 工作台 推进阶段 | 确认弹框→Toast | ✅(此前实测) |
| 渠道与分配 保存 | Toast「已保存·即时生效」 | ✅ eval 返回确认 |
| 主题切换 亮/暗/随系统 | 背景/EP 变量切换 | ✅ 暗色(auto)亮色 两态截图 |
| 语言切换 中/EN | 导航/顶栏/品牌翻译 | ✅ 中英两态截图(Workbench/Leads/Roles & Perms…) |
| 后台 角色与权限 | 矩阵 28 开关渲染 | ✅ eval rows=7/switches=28 |
| 后台 渠道与分配 | 渠道表+策略+数值 | ✅ eval rows=5/seg=1/num=3 |
| dist 静态打开 | 无 dev server 渲染+交互 | ✅ python http.server 实测 |

**结论:** demo 闭环完成,无遗留开口项(原型 = 设计)。视图正文仍以中文为主、i18n 已外化 shell/导航/顶栏(可扩展全量)——记为显式可扩展项。
