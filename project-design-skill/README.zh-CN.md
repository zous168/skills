<h1 align="center">🧭 Project Design Skill · 项目设计技能</h1>

<p align="center">
  <b>把一句话想法或详细需求,变成一整套生产级设计 —— 不写代码。</b><br>
  受 Devika 启发的流水线 · <code>创建 → 审核 → 优化</code> · 设计文档 + 可交互真实原型
</p>

<p align="center">
  <a href="README.md">English</a> · <b>中文</b>
</p>

---

一个 Claude Code **技能**:把简要或详细的项目需求,经过**逐步自动化流水线**,产出一整套
**生产级分析与设计文档**,外加可选的**可交互原型**;每个产物都走 **创建 → 审核 → 优化**。
**不产出业务代码**(原型是唯一刻意的例外)。

改编自 Devika 的智能体流水线(Planner → Researcher → Coder → Decision → Patcher →
Reporter):把 "Coder" 变为文档 **Writer**、"Patcher" 变为显式 **Reviewer**,并新增
**原型阶段**(frontend-design 生成 + "taste" 样式风格审核)。

## ✨ 产出什么

带版本头的设计集(每篇含 `doc_version` / `updated` / `status`):

| # | 文档 |
|---|------|
| 01 | 需求规格(PRD)+ 角色 × 权限矩阵 |
| 02 | 业务与领域分析 |
| 03 | 系统架构(含子系统分解、ADR) |
| 04 | 功能树(子系统 → 模块 → 页面 → 操作) |
| 05 | 数据设计(cfg / data / logs / stats) |
| 06 | 接口设计(REST + OpenAPI/Swagger、页面→接口映射) |
| 07 | 异步 / 后台处理(job / 异步 / 调度) |
| 08 | 非功能性需求(含 i18n / 主题 / 响应式) |
| 09 | 技术选型 |
| 10 | 工程目录结构(模块优先) |
| 11 | 风险评估 · 12 测试策略 · 13 路线图 · 14 任务计划(切片 + DoD) |

另含(条件):UX/交互说明、安全、隐私合规、集成/迁移、运维部署、成本容量、**缓存设计**;
在所选前端栈里、按生产标准构建的可交互**原型**(真实组件、页面+组件、**mock 数据置于可切换的数据源之后**、可用交互、编译 `dist/` 预览);
汇编阶段产出 `README` / `CHANGELOG` / `consistency-report`。

## 🔄 流水线

```
Phase 0 意图澄清   → 基线 + 技术方向(前端/UI + 图标库、后端、数据库、架构)
                     + 子系统(类型 · 美观度要求 · 参考效果图)
                     + 角色 + 默认项(主题/i18n/响应式)
Phase 1 规划       → 选哪些文档 + 是否做原型(按子系统)
Phase 2 调研       → 聚焦关键词(条件)
Phase 3 逐篇 创建→审核→优化
Phase 4 原型       → 生成(frontend-design)→ taste 审核 → 优化
Phase 5 一致性审核 → 跨文档 + 原型一致性 + 可追溯
Phase 6 汇编       → 索引、CHANGELOG、置版本
```

## 📁 结构

```
project-design-docs/
├── SKILL.md                      # 技能本体:流水线、原则、模板
├── REQUIREMENTS.md               # 需求 → 覆盖矩阵 + 迭代日志
└── references/
    ├── document-catalog.md       # 文档库(01–14 + 条件文档)
    └── review-rubrics.md         # 内容 / 逻辑 / 一致性 / taste 审核清单

examples/
└── crm-design-demo/              # 完整示例(CRM):文档 01–14 + Vue 原型
```

## 🚀 安装 / 使用

把 `project-design-docs/` 文件夹放进你的 Claude Code 技能目录(如 `~/.claude/skills/`),
或在项目里引用。然后对 Claude 说 "创建项目 / 设计一个系统 / 写份方案 / 出一套设计",技能即触发。

## 🔌 依赖(可选 · 仅原型阶段)

仅 **Phase 4(原型)** 用到;各有兜底,文档流水线两者都不需要。

| 技能 | 用途 | 仓库 | 安装 |
|------|------|------|------|
| **frontend-design**(Anthropic) | UI 视觉方向(Phase 4a) | [anthropics/claude-plugins-public](https://github.com/anthropics/claude-plugins-public/tree/main/plugins/frontend-design) | Anthropic 插件市场(添加 `anthropics/claude-plugins-public` → 安装 `frontend-design`) |
| **taste** = `design-taste-frontend`(Leonxlnx) | 反模板化样式审核(Phase 4b) | [Leonxlnx/taste-skill](https://github.com/Leonxlnx/taste-skill) | `npx skills add Leonxlnx/taste-skill` |

## 📄 许可

见 [LICENSE](LICENSE)。
