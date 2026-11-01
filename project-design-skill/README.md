<h1 align="center">🧭 Project Design Skill</h1>

<p align="center">
  <b>Turn a one-line idea or a detailed brief into a complete, production-grade design set — no code.</b><br>
  Devika-inspired pipeline · <code>create → review → optimize</code> · documents + a real interactive prototype
</p>

<p align="center">
  <b>English</b> · <a href="README.zh-CN.md">中文</a>
</p>

---

A Claude Code **skill** that turns a brief or detailed project requirement into a
complete set of **production-grade analysis & design documents** — plus an
optional **interactive prototype** — through an automatic, step-by-step pipeline
where every artifact goes **create → review → optimize**. No application code is
produced (the prototype is the one intentional exception).

Adapted from Devika's agent pipeline (Planner → Researcher → Coder → Decision →
Patcher → Reporter): the "Coder" becomes a document **Writer**, the "Patcher"
becomes an explicit **Reviewer**, and a **Prototype phase** (frontend-design +
a visual-style "taste" review) is added.

## ✨ What it produces

A versioned design set (each doc carries `doc_version` / `updated` / `status`):

| # | Document |
|---|----------|
| 01 | Requirements Spec (PRD) + roles × permission matrix |
| 02 | Business & Domain Analysis |
| 03 | System Architecture (+ sub-system decomposition, ADRs) |
| 04 | Feature Tree (sub-system → module → page → operation) |
| 05 | Data Design (cfg / data / logs / stats) |
| 06 | Interface / API Design (REST + OpenAPI/Swagger, page→endpoint map) |
| 07 | Async / Background Processing (jobs, async tasks, scheduled) |
| 08 | Non-Functional Requirements (incl. i18n / theming / responsive) |
| 09 | Technology Selection |
| 10 | Project Structure & Directory Design (module-first) |
| 11 | Risk Assessment · 12 Test Strategy · 13 Roadmap · 14 Task Plan (slices + DoD) |

Plus (conditional): UX/Interaction Brief, Security, Privacy/Compliance,
Integration/Migration, Ops/Deployment, Cost/Capacity, **Caching Design**;
an interactive **prototype** in the chosen front-end stack, built to production
standards (real components, pages + components, **mock data behind a swappable
source**, working interactions, a compiled `dist/` preview);
and `README` / `CHANGELOG` / `consistency-report` on assembly.

## 🔄 Pipeline

```
Phase 0 Intake & Clarify  → baseline + tech direction (FE/UI + icon lib, BE, DB, arch)
                            + sub-systems (type · aesthetic bar · visual refs)
                            + roles + defaults (theme/i18n/responsive)
Phase 1 Plan              → which docs + whether to prototype (per sub-system)
Phase 2 Research          → focused, keyword-driven (conditional)
Phase 3 Draft→Review→Optimize, per document
Phase 4 Prototype         → generate (frontend-design) → taste review → optimize
Phase 5 Consistency review→ cross-document + prototype coherence + traceability
Phase 6 Assemble          → index, CHANGELOG, set version
```

## 📁 Layout

```
project-design-docs/
├── SKILL.md                      # the skill: pipeline, principles, templates
├── REQUIREMENTS.md               # requirement→coverage matrix + iteration log
└── references/
    ├── document-catalog.md       # the document library (01–14 + conditionals)
    └── review-rubrics.md         # content / logic / consistency / taste rubrics

examples/
└── crm-design-demo/              # a full worked example (CRM): docs 01–14 + Vue prototype
```

## 🚀 Install / use

Copy the `project-design-docs/` folder into your Claude Code skills directory
(e.g. `~/.claude/skills/`), or reference it from your project. Then ask Claude to
"创建项目 / design a system / write a spec / 出一套设计方案" and the skill triggers.

## 🔌 Dependencies (optional — Prototype phase only)

Only **Phase 4 (Prototype)** uses these; each has a graceful fallback, and the
document pipeline needs neither.

| Skill | Purpose | Repo | Install |
|-------|---------|------|---------|
| **frontend-design** (Anthropic) | UI visual direction (Phase 4a) | [anthropics/claude-plugins-public](https://github.com/anthropics/claude-plugins-public/tree/main/plugins/frontend-design) | Anthropic plugin marketplace (`anthropics/claude-plugins-public` → install `frontend-design`) |
| **taste** = `design-taste-frontend` (Leonxlnx) | Anti-slop visual-style review (Phase 4b) | [Leonxlnx/taste-skill](https://github.com/Leonxlnx/taste-skill) | `npx skills add Leonxlnx/taste-skill` |

## 📄 License

See [LICENSE](LICENSE).
