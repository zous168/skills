---
name: project-design-docs
description: >-
  Turn a brief or detailed project requirement into a complete set of
  production-grade analysis and design documents — PRD, business/domain
  analysis, system architecture, data model, API/interface design,
  non-functional requirements, tech selection, risk assessment, test strategy,
  and roadmap — plus an optional interactive prototype. The design documents
  contain NO code; the only exception is the prototype — built in the project's
  chosen front-end framework (real components, pages + components, mock data),
  with the frontend-design skill informing the look and a visual-style ("taste")
  review judging it. Runs an automatic step-by-step pipeline where
  every document goes through draft → review → optimize, with the AI performing
  logic & content review, a prototype taste review, and cross-document
  consistency review, and asking the user to choose between options up front when
  the requirement is ambiguous. Use this whenever the user wants to 创建项目 /
  start a new project, plan or design a system or product, produce
  requirement/design/architecture documentation, write a PRD, spec, or 方案, build
  a prototype / 原型 / 交互原型 / clickable mockup for a project, or turn a
  one-line idea into structured design docs — even if they don't explicitly say
  "documents". Trigger for 设计文档, 需求文档, 需求分析, 架构设计, 方案设计,
  概要设计/详细设计, 原型设计, "帮我出一套方案", "我想做一个…", project design
  docs, "design a system for X", "write a spec for", "prototype this idea".
---

# Project Design Docs

Turn a project requirement (one sentence or many pages) into a coherent set of
**production-grade analysis & design documents** — the artifacts a real team
needs to start building — plus an optional **interactive prototype** that makes
the design tangible.

The analysis & design output is **documents (Markdown), with no code**. The
single, deliberate exception is the **Prototype phase**: a prototype has to be
seen and clicked, so it produces a runnable interactive prototype — **built in
the project's chosen front-end framework** (real components, pages + components,
mock data), or static HTML for a quick concept. That is the only place front-end
code is generated; its visual direction comes from a dedicated design skill
(`frontend-design`) and the result is judged by a visual-style review we call
**taste**.

The design is adapted from Devika's agent pipeline (Planner → Researcher →
Coder → Decision → Patcher → Reporter), with three deliberate changes: the
"Coder" becomes a **document Writer**; the "Patcher" becomes an explicit
**Reviewer** so every artifact is created, reviewed, and optimized rather than
produced once and trusted; and a **Prototype phase** is added (no Devika analog)
that builds a runnable prototype in the chosen framework (visual direction from
the `frontend-design` skill) and then runs a **taste** review, so the design is
validated as something you can look at, not only read.

## Operating principles

- **Step-by-step and resumable.** Work runs as an explicit pipeline tracked in
  a `00-PROGRESS.md` state file, so a long job can be paused and continued, and
  the user can always see where things stand. (This mirrors Devika's
  persisted `AgentState`.)
- **Clarify before committing, then run autonomously.** Ambiguity is cheapest
  to resolve at the start. Ask focused, optioned questions up front; once the
  baseline is agreed, drive the rest of the pipeline without nagging.
- **Never rubber-stamp — reviews are checklist-driven.** The review pass is a
  genuinely separate, critical read against rubrics — not a pat on the back for the
  draft you just wrote; approach it as a skeptical reviewer trying to find what's
  wrong. **Every review pass works through the *Skill review checklist*
  (`references/review-rubrics.md`)** — tick each item pass/fail with evidence and
  record the ticked list in the review trail — so a review is **auditable, not a
  vibe**, and nothing on the list is silently skipped.
- **Production-grade means actionable.** A document is done when a competent
  team could act on it without guessing: concrete, measurable, no hand-wavy
  placeholders, assumptions made explicit.
- **Match the user's language.** Write the documents in the language of the
  requirement input (Chinese in → Chinese docs, English in → English docs).
- **Make it tangible.** For user-facing products, a clickable prototype surfaces
  problems that prose hides. Build it in the chosen framework — with the
  `frontend-design` skill informing the look — then judge its visual quality with
  the taste review instead of accepting the first look — the same "never
  rubber-stamp" discipline, applied to design rather than text.
- **Assume the standard cross-cutting defaults.** Unless the user excludes them,
  a modern product UI is expected to support: **theming — light / dark / follow-
  system**; and **internationalization — bilingual by default (the user's language
  + English) on an i18n foundation that's extensible to more locales** (don't
  hard-code strings). Bake these into the requirements/NFR/UX/structure docs and
  demonstrate them in the prototype, rather than treating them as afterthoughts.
  Other near-universal defaults to assume unless told otherwise: responsive
  layout, accessibility (keyboard focus, contrast), and the empty/loading/error
  states.
- **Design docs are living, versioned artifacts.** A design set isn't written
  once — it's revised as the project evolves. Version every document, keep a
  changelog, and handle each requirement change as a controlled flow — capture →
  impact-analysis → update affected docs → re-validate (see the *Requirement-change
  workflow* under *Iteration & version management*) — so each iteration is
  trackable and you never silently overwrite an approved doc.
- **Work in independently-verifiable slices, gated.** Don't try to produce the
  whole set (or whole sub-system) in one undifferentiated pass. Each document — and
  each sub-system — is a slice that goes through its own create → review → optimize
  loop and is individually acceptable before the next dependent slice starts;
  read-only exploration can run in parallel, but delivery is serial along the
  dependency order. This is the same task-slicing discipline the design hands off
  to the build team in the Task Plan doc (14) — applied to the design work itself.

## The pipeline

```
Phase 0  Intake & Clarify   → confirmed requirement baseline + assumptions log
Phase 1  Plan               → which documents + whether to prototype, in order
Phase 2  Research            → focused, keyword-driven grounding (conditional)
Phase 3  Draft→Review→Optimize, per document, in dependency order
Phase 4  Prototype          → generate interactive prototype (via frontend-design
                             skill) → taste/style review → optimize; feed UX
                             findings back into the docs        (conditional)
Phase 5  Consistency review → cross-document + prototype coherence + traceability
Phase 6  Assemble & finalize→ index, statuses, open questions, handoff summary
```

Use a todo list (TaskCreate/TodoWrite if available) to track the phases so none
is skipped.

---

### Phase 0 — Intake & Clarify

Goal: produce a **requirement baseline** everyone agrees on before any document
is written.

1. **Read and classify** the requirement. Identify: project type (web app,
   mobile, API/service, data platform, internal tool, etc.), domain, rough
   scale, and obvious constraints.
2. **Detect what's missing.** Production design needs answers to questions like:
   target users & their context, the core problem and success metric, platform(s),
   expected scale (users / data / throughput), must-have vs nice-to-have scope,
   external systems/integrations, hard constraints (budget, deadline, tech stack,
   compliance), and which non-functional qualities matter most (latency, uptime,
   security, cost…).
3. **Decide: ask or assume.**
   - If the requirement is **brief or ambiguous on items that materially change
     the design**, use `AskUserQuestion` to prompt the user with concrete
     options (this is the "提示选择" the user expects). Batch related questions
     (≤4 per round), always give a recommended default as the first option, and
     keep options mutually distinct. Prefer choosing-from-options over open
     questions — it's faster for the user.
   - If the requirement is **detailed enough**, do not interrogate. Proceed, but
     **record every assumption you made** so they're visible and correctable.
4. **Settle the technical direction early — ask, don't silently assume.** Even
   when the *requirement* is detailed, the **tech stack is usually unstated**, and
   it ripples into the later artifacts that can't be done well without it:
   - the **prototype's visual style** — a prototype should look like the chosen
     UI library (Ant Design, Element Plus, shadcn/ui, Material…), not a generic
     mockup;
   - the **project/directory structure** — it's dictated by the framework
     (Next.js app router vs Vite/Vue, NestJS modules vs Spring packages); and
   - the **data design** — the datastore choice (relational vs document, and any
     separate analytics/cache store) shapes the whole data model.

   So unless the user already fixed the stack, use `AskUserQuestion` to settle, at
   minimum: **front-end framework + UI component library** (and its **icon
   library** — default to the UI library's own icon set, e.g. Element Plus Icons /
   Ant Design Icons / MUI Icons / Lucide for shadcn; if the framework ships none,
   pick an **open icon library** — Iconify, Lucide, Tabler, Heroicons — so the UI
   stays polished; this one has a sensible default, so only ask when the user has a
   preference), **back-end framework /
   language**, the **database(s)** (primary datastore — e.g. PostgreSQL / MySQL /
   MongoDB — plus any separate analytics/log store and a cache/queue like Redis;
   note how the cfg/data/logs/stats split maps onto it), and **architecture shape**
   (monolith / modular-monolith / microservices — it sets the back-end structure's
   split granularity). Give
   stack options that fit the project type, with a reasoned recommendation first.
   These become decisions in the baseline and feed the Data Design doc, the
   Tech-Selection doc, the Project-Structure doc, and the prototype. Skipping this
   is the most common way the output ends up generic.
5. **Confirm the sub-systems (client surfaces) — don't assume "one app".** Most
   real products are several surfaces: an **admin / back-office console**, a
   **public website / marketing site**, a **mobile app**, an **open API** for
   partners, etc. Ask which ones are in scope — it changes the screen inventory,
   the architecture, and the structure. **For each in-scope surface, also pin down
   its *usage scenario* and its *UI-aesthetic bar*** — a productivity/utility tool,
   an internal admin/back-office console, a brand-forward public website, a data
   dashboard, and a consumer mobile app have very different design centres of
   gravity. Use `AskUserQuestion` to settle, per surface: **what kind of product it
   is** and **how much visual polish it needs** (e.g. *functional & information-
   dense — efficiency over beauty* for an internal console, vs *brand-forward &
   highly polished* for a marketing site). Batch this into the **same
   `AskUserQuestion` round as the in-scope question** (respect the ≤4-per-round
   rule from step 3) — don't open a fresh interrogation; for a single-surface
   product it's only an extra question or two. This decision drives two things: the
   **visual direction** — which prototype generator leads (`taste` for marketing /
   landing surfaces, `frontend-design` for product UI; see Phase 4) — and the
   **functional layout** (information density, navigation pattern, how much screen
   goes to chrome vs content). Record the per-surface type + aesthetic bar in the
   baseline. For a **complex, multi-sub-system** product, **decompose**: produce a
   top-level system overview, then design each sub-system as its own task (its own
   screens, feature tree, and where needed its own doc folder). This is the
   Devika-style "break the big thing into steps" move — it keeps each part coherent
   and reviewable instead of one sprawling blur. Also confirm the **responsive
   targets** (which devices must be supported, and the breakpoints) since that's a
   real design constraint, not an afterthought.
6. **Ask for visual references — don't design the look blind.** When any surface is
   user-facing, ask the user whether they can share **reference material that
   anchors the look**: mockups or effect images (效果图) they already have,
   screenshots of products/competitors whose feel they want, a brand / style guide
   or an existing design system, or even a few "like this, not that" examples.
   Accept links, attached images, or descriptions. Record what they give — or note
   "none provided — direction inferred from the product type + aesthetic bar" — in
   the baseline; these references feed the UX brief's visual direction and the
   Phase 4 prototype generator (`frontend-design` / `taste`). A concrete reference
   is the single best defence against a generic, templated result.
7. **Write the baseline.** Create the output folder (default
   `./<project-slug>-design/`) and write `00-PROGRESS.md` (template below)
   containing: the restated requirement, the agreed scope, the **chosen tech
   direction**, the **sub-systems in scope** (each with its usage-scenario type,
   UI-aesthetic bar, and any visual references), the **roles** (formalized later in
   the requirements doc 01), the **defaults in force** (theme modes, i18n
   languages, responsive targets — note any the user excluded), the assumptions
   log, and the open-questions list.

When to ask vs assume — a guideline: ask when a wrong guess would force a
rewrite of a whole document (e.g. "is this multi-tenant?", "real-time or
batch?"); assume when the detail only affects wording or can be safely flagged
as an open question.

### Phase 1 — Plan (which documents)

This is the **Planner**. Decide the document set from project type and scale —
don't dump every template on a small project, and don't under-spec a large one.

1. Select documents from `references/document-catalog.md`. That file lists the
   **core set** (almost always included) and **conditional documents** with the
   condition under which each is warranted, plus each document's purpose,
   section structure, upstream dependencies, and production-grade bar.
2. Order them by dependency (e.g. requirements → domain → architecture →
   **feature tree** → data → API → **async/jobs** → NFR → tech selection →
   project structure → risk → test → roadmap → task plan). Downstream docs reuse
   decisions from upstream docs.
   - **For a multi-sub-system product, plan per sub-system.** Produce one
     top-level overview (system context + the sub-system list + the shared
     concerns), then run the document set (and prototype) for each sub-system that
     warrants it, under its own sub-folder. Don't force several sub-systems into
     one undifferentiated set of docs.
   - **Always include the Feature Tree** for anything beyond trivial — it's the
     module → page → operation map that everything else is checked against
     (coverage, prototype screens, API endpoints, role permissions). It's the
     cheapest way to make the design reviewable.
3. **Decide whether to prototype.** Include the Prototype phase (Phase 4) when
   the product is **user-facing with a non-trivial UI** — that's where a
   clickable mockup earns its keep. Skip it for headless services, pure APIs,
   data pipelines, or back-end-only work, and say why. When prototyping, also
   include the **UX / Interaction Design Brief** conditional document — it
   defines the screens and flows the prototype renders, so the prototype isn't
   invented from scratch.
4. **Present the plan** to the user briefly (the list + one line each on why
   included/excluded, and whether you'll prototype) and let them add/drop. If the
   user said "just go ahead," skip the confirmation and proceed with the
   default-for-scale set.
5. Seed `00-PROGRESS.md` with one row per planned document (status `pending`) and
   a Prototype row if Phase 4 is in scope.

### Phase 2 — Research (conditional, focused)

This is the **Researcher**. Only research where it materially improves the
output — domain norms, regulatory/compliance requirements, technology trade-offs,
or unfamiliar problem spaces. Skip it for well-understood, self-contained
projects and say so.

- Extract a **small set of focused keywords/questions** first (Devika's
  keyword-driven approach), then research — don't search broadly and hope.
- If `WebSearch`/`WebFetch` are available, use them and cite sources. If not,
  ground in your own knowledge and **flag claims that should be verified** as
  open questions rather than asserting them as fact.
- Capture findings in `research-notes.md` (kept as working notes, referenced by
  later docs). Keep it tight; this is grounding, not a literature review.

### Phase 3 — Draft → Review → Optimize (per document)

For each planned document, in dependency order, run all three sub-steps before
moving on. This is the core create/review/optimize loop the user asked for.

**3a — Create (Writer).** Draft the document using its template and bar from
`references/document-catalog.md`. Open it with the **version frontmatter**
(`doc_version: 1`, `updated:` today's absolute date, `status: draft`). Pull facts
from the baseline, research notes, and already-finished upstream documents —
re-derive nothing, contradict nothing. Fill every section with substance; if a
section genuinely doesn't apply, write "N/A — <reason>" rather than leaving a
hole. Update status → `drafted`.

**3b — Review (Reviewer).** Switch hats and read the draft critically against
the **content** and **logic** rubrics in `references/review-rubrics.md`. **Work
through the *Per-document* section of the Skill review checklist there** — tick each
item pass/fail/N-A with evidence, and record the ticked checklist in the
`00-PROGRESS.md` review trail. Produce a findings list (severity
blocker/major/minor, location, and a concrete fix for each). For larger or higher-stakes projects, get independence by spawning a
subagent to do this review (give it the draft + the rubric file + the upstream
docs); otherwise do a deliberate, skeptical self-review pass. The goal is to
find real problems — a review that finds nothing on a first draft is a yellow
flag that the review wasn't critical enough. Update status → `reviewed`.

**3c — Optimize.** Apply the fixes. Re-check that blockers and majors are
actually resolved, not just acknowledged. Append a short "Review notes" trail to
`00-PROGRESS.md` (what was found, what changed) so the trail is auditable.
Update status → `optimized`.

### Phase 4 — Prototype (conditional)

Run this only if Phase 1 put prototyping in scope. The goal is a clickable,
visually-considered prototype of the key screens and flows — the most tangible
way to validate the design before anyone builds it. It runs the same
create → review → optimize discipline as the documents, but the "review" here is
the **taste** review (visual style), and the artifact is code, not prose.

**4a — Generate.** Build the prototype using the **`frontend-design`** skill for
visual direction (palette, type, layout) and the **chosen framework's real
components** for the build itself. Don't start from a blank brief — ground it in
the work already done, including **the visual references the user supplied and
each surface's declared aesthetic bar** (Phase 0): a brand-forward marketing
surface and an efficiency-first internal console should look and lay out
differently, and a provided reference image is the strongest anchor against a
templated default. Build it to **production standards** — the real framework, a
production-style structure, and data behind a **swappable source** — so it's a
faithful starting point, not a throwaway demo. These points make the difference
between a real prototype and a toy:

1. **List the screen inventory first, then build each — across every in-scope
   sub-system.** Derive the screens from the functional modules in the
   requirements (one product with ten modules needs more than one screen), and
   **group the inventory by sub-system** (admin console, public website, mobile
   app, …) — the sub-systems confirmed in Phase 0. Each in-scope sub-system that
   has a UI gets its key screens prototyped. If you deliberately defer a
   sub-system's screens this round (e.g. only the operator workbench first, admin
   later), that's allowed — but **record it as an explicit, reasoned deferral** in
   `prototype-notes.md` and the plan; **don't silently omit a sub-system** (that's
   how "where's the admin/app?" gaps happen — they should be a stated decision,
   not an accident the consistency review has to catch). Map each screen to its
   FR/module, tie them together with a shared shell/navigation, scope each to its
   MVP, but don't collapse ten modules into one page.
2. **Build it in the *actual* chosen framework — not a look-alike.** When Phase 0
   settled a front-end framework + UI library, build the prototype as a **real
   project in that stack with its real components** (e.g. a Vite + Vue 3 +
   Element Plus app using `el-table`/`el-dialog`/`el-drawer`/`ElMessage`; or
   React + Ant Design with `antd` components). A hand-written HTML page that
   merely *looks* like the library is a last resort — using the real components is
   what makes the prototype faithful to what gets built, and it's faster than
   re-deriving the look. **Trade-off to state up front:** a framework prototype
   needs `install + dev server` to run (not a double-click) — include the run
   command (e.g. `npm install && npm run dev`).
   **Also ship a compiled preview.** Run the production build (`npm run build` →
   `dist/`) configured to open *statically* — for Vite, set `base: './'` (relative
   asset paths) and use hash routing — so a reviewer can open the built HTML
   directly (file:// or any static server) **without the toolchain**. `dist/` is
   the shareable preview / handoff artifact; the dev server is for live editing.
   (For the static-HTML fallback there's nothing to compile — it's already the
   preview.)
3. **Structure as pages + reusable components.** Don't put everything in one giant
   file — it gets unmaintainable and unreviewable. Split into **views/pages**
   (one per screen, matched to the feature tree) and **reusable components**
   (shell/nav, KPI strip, tables, the bits shared across pages), with a **router
   whose route tree covers every screen — including nested / child routes**
   (master→detail, tabbed sub-sections), **each route resolving to a real implemented
   page** (no placeholder routes, no page left unrouted), and a
   **data-access layer** organized in three parts: an **`apiClient`** (the one
   configured HTTP client — base URL, interceptors, a **unified auth layer** that
   attaches the **token** (`Authorization: Bearer …`) from a token store and handles
   **401 → refresh / redirect to login**, and a consistent error envelope — the
   single swap point), **one API module per resource** (`api/<module>.js`,
   grouping that module's calls), and an **aggregate `api`** index (`api/index.js`)
   that composes the modules. Views and components **import the aggregate `api` and
   call `api.<module>.<method>(args)`** — e.g. `api.leads.list(params)`,
   `api.opportunities.close(id, payload)` — never a raw `fetch`/`axios` in a
   component, and never the mock fixtures directly. The **mock data sits behind
   `apiClient` as a swappable source**. This mirrors the real project structure
   (doc 10) and keeps each file small.
4. **Mock data, but real interaction.** Use mock/fixture data (no backend), yet
   the interactions must work: **confirm/modal dialogs** on gated or destructive
   actions, **toast/notification** feedback, **drawers/popovers** where the
   library uses them, **navigation and drill-down linkage between screens**
   (clicking a list row opens its detail; a card jumps to the related screen), and
   the **empty / loading / error** states — not only the happy, populated path.
   **Put create/edit forms in the library's modal dialog or drawer** (`el-dialog` /
   `el-drawer`, antd `Modal` / `Drawer`) rather than a separate full-page form —
   that's the idiom these libraries are built around and it keeps the user in
   context on the list/workbench; reserve a full page or a multi-step **wizard**
   only for genuinely long/complex input. Wire the primary action buttons (incl.
   create/new/edit) so a reviewer can actually open the form dialog, see it
   validate, submit, and get the toast/feedback — and trigger the confirm→feedback
   loop on destructive actions. Use the library's own primitives (e.g.
   `ElMessageBox`, `ElMessage`) rather than reinventing them.
5. **Demonstrate the cross-cutting defaults.** Unless excluded, wire in **theme
   switching (light / dark / follow-system)** using the library's dark-mode
   mechanism, and **language switching** on an **i18n foundation** (e.g.
   vue-i18n + the library's locale via `el-config-provider`; react-i18next +
   antd `ConfigProvider`) with the user's language + English — strings come from
   the i18n catalog, not hard-coded. These prove the system's default
   capabilities are real, not just claimed in a doc.
6. **Use the chosen icon library — icons, not emoji or bare text.** Render icons
   from the **icon library settled in Phase 0**: the UI library's own set (Element
   Plus Icons, Ant Design Icons, MUI Icons, Lucide for shadcn) or, when the
   framework ships none, the agreed **open library** (Iconify / Lucide / Tabler /
   Heroicons). Use them purposefully — nav items, action buttons, status, empty
   states — for clarity and a polished look, **one consistent family at consistent
   sizes**. Don't substitute emoji or text glyphs for real icons, and don't mix
   clashing icon sets.
7. **Build to production standards — mock behind a swappable data source.** The
   prototype isn't throwaway: build it to the **same engineering standards as
   production** so it can become the real front-end. Route **all data access through
   the aggregate `api` → per-module methods → `apiClient`**, with the **mock wired in
   behind `apiClient`** (or as the api modules' current implementation), returning
   exactly the shapes the **page→interface map (doc 06)** documents — same fields,
   types, pagination, and error envelope; **each api method corresponds to a doc-06
   endpoint**. Then **going to real development is switching the data source at
   `apiClient`** (point it at the live `/api/v1`) — the call sites stay
   `api.<module>.<method>(args)`, unchanged — **rather than a rewrite**. The
   **auth / token mechanism lives in `apiClient`** the same way — a request
   interceptor **attaches the token** and a **401 handler** refreshes it or routes to
   login — **framework-idiomatic** (e.g. an axios interceptor + a Pinia/Vuex auth
   store in Vue; a fetch wrapper + context/store in React) and wired with a **mock
   token / mock login** in the prototype; production swaps in the real token endpoint.
   This is production *standard*, not production *scope*: it stays front-end-only,
   mock-data, MVP-scoped — don't add a backend, a **real auth/identity provider**, or
   features beyond the key flows (the apiClient **token plumbing with a mock token is
   expected**; a real IdP / login backend is not); the discipline is in the
   *structure*, not extra features.

Pull content from finished upstream docs (entities/fields from the data design so
forms/lists show realistic mock content, not lorem ipsum), and make each screen's
**displayed data and actions correspond to the endpoints in the page→interface map
(doc 06)** — the prototype is where that map gets validated against a real screen,
surfacing any read or action endpoint the API design still misses. Annotate each
screen with the requirement IDs (FR-…) it realizes so the prototype stays traceable.

> **Fallback:** if no framework was chosen (quick concept, or a pure
> landing/marketing page), a single-file or static `index.html` + shared
> `app.css`/`app.js` is fine and opens with a double-click. Reserve it for that
> case; for a product with a settled stack, prefer the real framework.

**Verify by clicking through — structure-driven acceptance, not just "it renders."**
**Start from the structure, not a random click-around.** First enumerate the
**route tree** — top-level routes and **every nested / child route** (master→detail,
tabbed sub-sections) — and confirm **each route resolves to a real, implemented
page** (no route 404s, no placeholder/empty page, no page built but unrouted). Then
walk that tree: for each page, take its **operations and the data structures they act
on** (from the feature tree + page→interface map + data design) and verify each is
actually wired. Run the prototype and **actually click every key interactive
element**, confirming the expected effect — don't accept it by eyeballing a
screenshot. Build a **structure-driven acceptance checklist** in
`prototype-notes.md`, organized **route → page → element**: one row per interactive
element with its expected result, ticked when exercised. At minimum, per page: each
**route / sub-route → its page renders**; **tab / segmented → its panel switches and
loads**; each **action button → its dialog/drawer opens**; **create/edit → form
dialog opens, validates, submits → toast fires**; **confirm → toast/feedback fires**;
**list row / card → drills into detail / navigates**; **state / status toggle → the
state actually changes and reflects back**; **filters, theme & language toggles →
switch**; the **empty / loading / error** states are reachable. **No dead ends** —
every route, sub-route, tab, button, and state toggle must reach a real front-end
implementation; a control that renders but does nothing (dead link, dead tab, inert
button, non-switching toggle) is a defect, not a stub. Drive the clicks with the
browser/preview tools (`preview_click` / `preview_eval`) so acceptance is evidence,
not assertion. Also: run across the declared **responsive breakpoints**, and confirm
the compiled `dist/` opens **statically** (no dev server) and still renders +
interacts. A prototype with a **dead route, unrouted page, dead tab, inert button, or
non-switching toggle** — whose interactions weren't click-verified — or whose compiled
preview is blank (usually wrong `base`/router mode) — isn't accepted.

> If `frontend-design` isn't enabled, enable the `frontend-design` plugin or
> apply its principles directly. **Choosing the generator by screen type:** the
> `taste` skill (below) is scoped to **landing pages, portfolios, and marketing /
> editorial screens** — it explicitly does *not* target dashboards, data tables,
> or multi-step product UI. So for a product's marketing/landing screen, let taste
> generate it directly; for the product UI itself (forms, tables, wizards), use
> `frontend-design` to generate and apply taste only as the review lens (4b).
> Either way the prototype must be real and openable — not a screenshot or a
> wireframe-only doc.

**4b — Taste review (visual-style review).** This is the **taste** step — the
visual-style review the user asked for. Drive it with the **`taste` skill**
(`design-taste-frontend`, from https://www.tasteskill.dev/), an anti-slop
frontend framework whose whole job is catching generic, templated AI design.
Install it once if it isn't present:
>
>     npx skills add Leonxlnx/taste-skill
>
Point taste at the generated `prototype/` and run its **pre-flight check** /
**anti-default discipline** over the screens (taste's `redesign-skill` is the
audit-first variant aimed at critiquing existing UI — useful here). Its scope
caveat above applies to *generation*, not review: its anti-slop principles judge
any UI fairly. Capture screenshots first if the environment supports it (a picture
is worth far more than a paragraph here). **Work through the *Prototype* section of
the Skill review checklist (Rubric D)** and record the **ticked checklist** in
`prototype-notes.md` alongside its findings, with the usual severity levels — so the
prototype review is auditable, not a vibe. For higher-stakes work, run
this as an independent pass (a subagent given the prototype + the taste skill) so
it's a genuine critique, not self-congratulation.

If the taste skill can't be installed in this environment, fall back to **Rubric
D — Taste** in `references/review-rubrics.md`, which distills the same anti-slop
criteria into a manual checklist. Either way, the question is the same: does this
look like a deliberate design for *this* product, or like a templated default?

**4c — Optimize.** Apply the taste findings (re-invoking the generator —
`frontend-design` or `taste` — as needed), and also do a quick **functional**
check: do the prototyped flows cover
the key requirements, and does the prototype contradict any document (a field
not in the data model, a step not in the flow)? Anything that should change a
document — a renamed flow, a missing field, a clarified interaction — gets noted
in `prototype-notes.md` and **fed back into the affected documents**; Phase 5
then reconciles everything. Update the Prototype status → `optimized`.

### Phase 5 — Consistency review (cross-document + prototype)

Once all documents are optimized (and the prototype, if any), do a whole-set
pass — the single most valuable step for production quality, because
individually-fine artifacts routinely disagree with each other. **Work through the
*Consistency* section of the Skill review checklist (Rubric C)** in
`references/review-rubrics.md`, ticking each item with evidence and recording it in
`consistency-report.md`. Check at minimum:

- **Terminology**: the same concept is named the same way everywhere; a glossary
  covers key terms.
- **Entity/data alignment**: entities in the data model match those referenced in
  requirements and the API/interface design (names, attributes, relationships).
- **Scope alignment**: no document promises something another puts out of scope.
- **Decision coherence**: architecture, tech selection, and NFRs don't contradict
  (e.g. an NFR demanding strong consistency vs an architecture chosen for
  eventual consistency).
- **Traceability**: every functional requirement maps to a design element and to
  a test-strategy entry; flag orphans in both directions.
- **Prototype alignment** (if Phase 4 ran): screens, fields, and flows in the
  prototype match the requirements, data model, and UX brief — no field or step
  exists only in the prototype, and no key flow the prototype shows is missing
  from the docs.

Write `consistency-report.md`, fix the issues in the affected documents
(re-running their optimize step), and only then mark the set consistent.

### Phase 6 — Assemble & finalize

This is the **Reporter**. Make the set easy to hand off.

1. Write/refresh `README.md` (index template below): one-paragraph project
   overview, the document list with links and statuses, the assumptions log,
   remaining open questions, and a **revision-history table**.
2. Set the **set version** (v1.0 for the first complete baseline) and write/append
   `CHANGELOG.md`. Confirm each doc's frontmatter `doc_version`/`updated`/`status`
   is current.
3. Mark all rows in `00-PROGRESS.md` complete.
4. Give the user a concise summary: what was produced, the key design decisions
   and why, the assumptions that need their sign-off, and the open questions.

---

## Iteration & version management

A design set is a living artifact — requirements change, scope shifts, the
prototype surfaces issues. Make every iteration trackable so nothing is silently
overwritten and a reviewer can see what changed.

**Requirement-change workflow.** Requirements *will* change after the baseline is
set — a new scenario, a dropped feature, a shifted constraint. Handle every change
as a controlled flow, not an ad-hoc edit, so nothing drifts out of sync:

1. **Capture & log.** Record the change request itself — what's changing, who asked,
   and why — as a dated row in the `00-PROGRESS.md` review trail (and an open item
   if it isn't agreed yet). Don't start editing until the change is written down.
   The *resolved* change is what lands in `CHANGELOG.md` at step 6 — that file is
   version-keyed (`## v1.x — date`), so it holds released changes, not pending
   requests.
2. **Classify.** Minor (wording, an added field, a clarified rule) vs major (a new
   scenario, a scope / architecture / stack shift, a new sub-system). This sets the
   version bump (see *Bump rules*) and how wide the blast radius is.
3. **Impact analysis — trace before you touch.** Walk from the change to every
   affected artifact, using the **Feature Tree (04)** as the index: which FRs,
   feature-tree nodes, entities/fields, API endpoints, async tasks, NFRs, tests,
   and prototype screens does it touch? List them *before* editing — a requirement
   change almost always ripples well past the requirements doc.
4. **Update the affected docs only.** Re-run each affected document through its
   create → review → optimize loop; bump its `doc_version` + `updated`; leave
   untouched docs exactly as they are (don't churn approved content). Update the
   prototype if a touched screen/flow changed.
5. **Re-validate the whole set.** **Always re-run the Phase 5 consistency review** —
   a local change is exactly how cross-document coherence breaks (a field the API
   now references, a renamed flow, a role that gained an operation).
6. **Version & record.** Append the resolved change to `CHANGELOG.md` with the new
   set version and the driver, and refresh the README revision-history table
   (Phase 6).

This uses the same machinery as *Revision mode* below — the workflow is just the
disciplined order to apply it in when a requirement moves.

**Per-document version frontmatter.** Every generated doc opens with:
```yaml
---
doc_version: 1
updated: 2026-06-19      # absolute date, not "today"
status: draft            # draft | reviewed | approved
---
```
Bump `doc_version` and `updated` whenever the doc materially changes in a later
iteration; move `status` forward as it's reviewed/approved.

**Set-level version + `CHANGELOG.md`.** The whole set carries a version — **v1.0**
for the first complete baseline, **v1.x** for minor revisions, **v2.0** for a
major re-scope. Record every iteration:
```markdown
## v1.1 — 2026-06-20
- 03 architecture: added the open-API sub-system (requirement change)
- 06 interface: +4 endpoints; OpenAPI spec regenerated
- 04 feature tree + 11 test: updated to cover the above
## v1.0 — 2026-06-19
- Initial complete design set
```

**Revision mode — re-running on an existing set.** In Phase 0, detect whether a
design set already exists in the output folder. If it does, run *incrementally*
rather than from scratch:
- Re-baseline only what changed (new/changed requirements, a scope or stack shift).
- Touch only the **affected** documents; bump their `doc_version`; leave untouched
  docs exactly as they are (don't churn approved content).
- Append the change to `CHANGELOG.md` and the `00-PROGRESS.md` review trail.
- **Always re-run the Phase 5 consistency review** — a local edit easily breaks
  cross-document coherence (an added field, a renamed flow). This reuses the
  resumable-pipeline idea: `00-PROGRESS.md` + the versions tell you what's current.

**Bump rules.** Wording/added detail/fixes → minor (`v1.x`, bump doc_version).
Scope / architecture / stack change → major (`v2.0`), and name the driver in the
changelog. The prototype is versioned the same way (note its revision in
`prototype-notes.md`).

Phase 6 refreshes a short **revision-history table** in `README.md` (version,
date, one-line summary) and sets the current set version.

---

## File layout produced

```
<project-slug>-design/
├── README.md                      # index / overview / open questions / revision history
├── CHANGELOG.md                   # set version history (v1.0, v1.1, …)
├── 00-PROGRESS.md                 # pipeline state + review trail
├── research-notes.md              # if Phase 2 ran
├── consistency-report.md
├── 01-requirements-spec.md         # each doc opens with version frontmatter; incl. roles & permission matrix
├── 02-domain-analysis.md
├── 03-system-architecture.md       # incl. sub-system decomposition & context
├── 04-feature-tree.md              # subsystem → module → page → operation (+roles)
├── 05-data-design.md               # organized as cfg / data / logs / stats DBs
├── 06-interface-api-design.md      # API inventory + per-endpoint + OpenAPI/Swagger
├── 07-async-background-processing.md # job workers, async tasks, scheduled/cron
├── 08-non-functional-requirements.md
├── 09-tech-selection.md
├── 10-project-structure.md         # directory/module layout — framework-driven
├── 11-risk-assessment.md
├── 12-test-strategy.md
├── 13-roadmap.md
├── 14-task-plan.md                 # sliced task backlog (cards) + closed-loop DoD
├── prototype-notes.md             # if Phase 4 ran: screen inventory + taste findings
└── prototype/                     # if Phase 4 ran: interactive prototype
    # ── framework build (preferred when a stack was chosen) ──
    ├── package.json  vite.config.js  index.html   # dev: npm install && npm run dev
    ├── src/
    │   ├── main.js  router/  styles/
    │   ├── api/                   # data-access layer
    │   │   ├── client.js          #   apiClient — base URL, interceptors, auth/token (attach Bearer, handle 401), error envelope (swap point)
    │   │   ├── <module>.js        #   per-resource api modules (leads, opportunities…)
    │   │   └── index.js           #   aggregate `api` → views call api.<module>.<method>(args)
    │   ├── mock/                  # mock source behind apiClient — swap for the real backend
    │   ├── components/            # reusable: shell/nav, KPI strip, tables…
    │   └── views/                 # one page per screen (matches the feature tree)
    └── dist/                      # compiled preview (npm run build) — opens statically, no dev server
    # ── OR static fallback (no chosen stack / landing page): index.html + app.css/app.js
```

For a **multi-sub-system** product, nest per sub-system instead:
```
<project-slug>-design/
├── README.md  00-PROGRESS.md  00-system-overview.md   # context + sub-system list
├── admin/      { 01-requirements … 12-roadmap, prototype/ }
├── website/    { … }
└── open-api/   { … }
```

(Number/keep only the documents the plan selected. Localize filenames to the
doc language if the user prefers, but keep the numeric prefixes for ordering.
The `prototype/` folder is the one part of the output that contains front-end
code — by design; everything else is code-free.)

## `00-PROGRESS.md` template

```markdown
# Project Design — Progress

**Set version:** v1.0  (see CHANGELOG.md)
**Requirement (restated):** <one paragraph>
**Scope (agreed):** <in-scope bullets> / **Out of scope:** <bullets>
**Sub-systems:** <surface — usage-scenario type (admin / tool / website / mobile…) · UI-aesthetic bar · visual references or "none">
**Defaults in force:** theme(light/dark/auto) · i18n(zh+en) · responsive(…)  — note any excluded

## Assumptions (need user confirmation)
- A1: <assumption> — basis: <why> — impact if wrong: <…>

## Open questions
- Q1: <question> — blocking: <doc(s)> / non-blocking

## Documents
| # | Document | Status | Notes |
|---|----------|--------|-------|
| 01 | Requirements Spec | pending\|drafted\|reviewed\|optimized\|done | |
| … | | | |
| PT | Prototype (if in scope) | pending\|generated\|taste-reviewed\|optimized\|done | |

## Review trail
- [01] review: checklist <n/n passed; failed items> — <findings summary> → <fixes applied>
- [PT] taste review: checklist <n/n passed; failed items> — <findings> → <fixes; docs fed back>
```

## `README.md` (index) template

```markdown
# <Project> — Analysis & Design

<1–2 paragraph overview: problem, who it's for, the chosen approach in brief.>

## Documents
1. [Requirements Spec](01-requirements-spec.md) — <one line>
2. …

## Key decisions
- <decision> — because <rationale>

## Assumptions to confirm
- <assumption>

## Open questions
- <question>

## Revision history
| Version | Date | Summary |
|---------|------|---------|
| v1.0 | 2026-06-19 | Initial complete design set |
```

---

## Reference files

- `references/document-catalog.md` — the document library: for each document,
  its purpose, when to include it, upstream dependencies, section-by-section
  structure, and what separates a production-grade version from a superficial
  one. **Read it in Phase 1** to choose the set, and again per document in
  Phase 3a for that document's template.
- `references/review-rubrics.md` — the **Skill review checklist (master index)** that
  every review pass walks and records, plus the content, logic, consistency, **and
  taste** rubrics behind it and the findings format and severity definitions. **Read
  it in Phase 3b (content/logic), Phase 4b (taste), and Phase 5 (consistency).**
- `frontend-design` **skill** (external) — the UI generator used in Phase 4a.
- `taste` **skill** = `design-taste-frontend` (external) — the Phase 4b taste review.

## Dependencies (external skills)

Both are **optional** — only the **Prototype phase (Phase 4)** uses them, and each
has a graceful fallback. The document pipeline (Phases 0–3, 5–6) needs neither.

| Skill | Purpose | Git | Install | Fallback if absent |
|-------|---------|-----|---------|--------------------|
| **frontend-design** (Anthropic) | UI visual direction for the prototype (Phase 4a) | https://github.com/anthropics/claude-plugins-public (`plugins/frontend-design`) | Anthropic plugin marketplace: add `anthropics/claude-plugins-public`, install `frontend-design` | apply its design principles inline |
| **taste** = `design-taste-frontend` (Leonxlnx) | Anti-slop visual-style review for the prototype (Phase 4b); can also generate landing/marketing screens | https://github.com/Leonxlnx/taste-skill | `npx skills add Leonxlnx/taste-skill` | **Rubric D** in `review-rubrics.md` (manual checklist) |

## Scope guardrails

- **Analysis & design documents contain no code** — no source, no DDL, no
  runnable scripts. Data and API designs are expressed as structured tables and
  described contracts, not as `CREATE TABLE` statements or code. Pseudo-flow and
  diagrams (Mermaid/ASCII) are fine and encouraged.
- **The prototype is the one intentional exception.** Phase 4 produces a runnable
  front-end prototype — a real project in the chosen framework (or static HTML for
  a quick concept) — so the design can be clicked through. Build it to **production
  standards** (real components, production-style structure, data behind a
  **swappable source** keyed to the documented endpoints) so the front-end is a
  faithful starting point — going to real development is **switching the mock source
  for the real API**, not a rewrite. That's production *standard*, not production
  *scope*: it stays a front-end, mock-data, MVP-scoped validation artifact — the
  apiClient **auth/token plumbing (with a mock token) is expected**, but don't build a
  backend, a **real auth/identity provider**, or features beyond the key flows, keep
  the code confined to `prototype/`, and don't let its implementation detail leak back
  into the design documents.
- Don't invent facts to fill a template. If something is unknown, it's an
  assumption or an open question — say so.
- Keep the loop honest: a document isn't `optimized` until its blocker/major
  findings are actually fixed, the prototype isn't `optimized` until its taste
  blockers/majors are fixed, and the set isn't `done` until the consistency
  report is clean.
