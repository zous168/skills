# Document Catalog

The library of analysis & design documents for software/product projects. Use
this in **Phase 1** to select the set, and in **Phase 3a** to draft each
document against its structure and bar.

## How to select

- **Core set** — include unless there's a clear reason not to. These cover any
  non-trivial software/product project.
- **Conditional documents** — include only when the stated condition holds.
  Adding an empty or speculative document hurts more than omitting it.
- **Scale dial.** For a small/internal tool, several core documents can collapse
  into shorter sections of one file (e.g. fold data + interface design into
  architecture). For a large or regulated system, conditional documents become
  near-mandatory. State your sizing choice in the plan.

Contents:
- [Core documents](#core-documents) (01–14)
- [Conditional documents](#conditional-documents)
- [Prototype (Phase 4 artifact)](#prototype-phase-4-artifact--not-a-document)
- [Production-grade bar — general](#production-grade-bar--general)

---

## Core documents

### 01 — Requirements Specification (PRD)
- **Purpose:** the single source of truth for *what* is being built and *why*.
- **Upstream:** the requirement baseline + clarifications from Phase 0.
- **Structure:**
  1. Background & problem statement (the pain, today's workaround)
  2. Goals & success metrics (measurable — e.g. "cut checkout time 30%")
  3. Target users / personas & key scenarios
  4. Scope: in-scope, explicitly out-of-scope
  5. Functional requirements (numbered FR-1…, each testable)
  6. User stories / use cases with acceptance criteria
  7. **System roles & permissions** — the actor/role list (e.g. admin, operator,
     end-user, service account) and a **role × capability matrix**; every gated
     operation names the role(s) allowed. These roles are reused by the Feature
     Tree and the API design.
  8. Assumptions, dependencies, constraints
  9. Glossary
- **Production-grade bar:** every requirement is uniquely IDed, testable, and
  prioritized (e.g. MoSCoW). No requirement is a solution in disguise. Success
  metrics are quantified. Out-of-scope is explicit enough to prevent scope creep.
  Roles are explicit and every restricted capability maps to at least one role.

### 02 — Business & Domain Analysis
- **Purpose:** model the problem domain so the design reflects real business
  rules, not just screens.
- **Upstream:** 01.
- **Structure:**
  1. Domain overview & key concepts
  2. Domain model (entities, relationships, ownership) — diagram + glossary
  3. Core business processes / workflows (with flow diagrams)
  4. Business rules & invariants (numbered BR-1…)
  5. States & lifecycles of key entities
  6. Stakeholders & their concerns
- **Production-grade bar:** the domain model is internally consistent and uses
  ubiquitous language reused verbatim downstream. Business rules are explicit and
  testable, not buried in prose.

### 03 — System Architecture Design
- **Purpose:** how the system is structured to meet functional + non-functional
  needs.
- **Upstream:** 01, 02, 08 (NFRs inform architecture — iterate if needed).
- **Structure:**
  1. Architecture goals & drivers (which NFRs shape it)
  2. Context view (system + external actors/systems) — C4-style context diagram
  3. **Sub-system decomposition** — the client surfaces in scope (admin console,
     public website, mobile app, open API…), each one's responsibility, and what
     back-end services / databases they share; for a complex product each
     sub-system is then designed as its own task
  4. Logical view: components/services, responsibilities, dependencies
  5. Key runtime flows (sequence diagrams for the 2–3 critical scenarios)
  6. Deployment view (environments, topology) — described/diagrammed
  7. Cross-cutting concerns (auth, logging, error handling, config, **caching** —
     detail in the Caching Design doc)
  8. Architecture Decision Records (ADR): decision, alternatives, rationale,
     consequences — one per significant choice
- **Production-grade bar:** each major component has a single clear
  responsibility; the diagrams and text agree; every significant decision is an
  ADR with alternatives considered (not just the chosen one); the architecture
  visibly traces back to specific NFR drivers.

### 04 — Feature Tree (功能树)
- **Purpose:** the hierarchical map of everything the system does —
  **sub-system → module → page → display + operation** — so the design is reviewable
  at a glance and nothing slips. It's the master coverage checklist that the
  prototype, the API inventory, and the tests are all cross-checked against.
- **Upstream:** 01 (FRs + roles), 03 (sub-systems & modules).
- **Structure:**
  1. The tree itself: per sub-system → its modules → each module's pages/screens →
     and under each page **both what it displays** (the data the screen shows /
     reads — lists, detail, metrics, lookups) **and its operations** (the actions a
     user can take). Capturing display *and* action — not operations alone — is what
     lets the API design derive every **read** endpoint a page needs, not just the
     write endpoints (this feeds the page→interface map in doc 06).
  2. Each leaf — a **display item or an operation** — annotated with: the
     **role(s)** allowed (from 01), the **FR id(s)** it satisfies, and — once known —
     the **API endpoint(s)** it maps to (a display item → its **read/query
     endpoint**, an operation → its **action endpoint**) and the **prototype
     screen**.
  3. Rendered as an indented list or a table so a reviewer can tick coverage
     line by line.
- **Production-grade bar:** every FR appears somewhere in the tree; every leaf
  (display item or operation) has a role and (eventually) an endpoint + screen —
  each page's display data mapping to a **read** endpoint and each operation to an
  **action** endpoint; the tree matches the prototype's screen inventory and the API
  inventory with **no orphans**. This is the artifact the consistency review
  (Phase 5) checks the whole design against.

### 05 — Data Design
- **Purpose:** the data the system holds and how it's structured — **as models,
  not DDL.**
- **Upstream:** 02 (domain model), 01.
- **Structure:**
  1. Conceptual data model (entities + relationships diagram)
  2. **Database partitioning** — organize the schema across the standard four
     logical databases: **`cfg`** (configuration / metadata / rules / dictionaries),
     **`data`** (core business entities & transactions), **`logs`** (audit,
     operation & event logs), **`stats`** (aggregates / reporting / metrics).
     State which entities live in which database and why. These four are *logical*
     databases — **map them onto the datastore(s) chosen in Phase 0** (e.g. four
     schemas in one PostgreSQL; or specialized stores: logs/stats in
     ClickHouse/ES, cache/queue in Redis). Name the physical mapping.
  3. Logical model: per entity, a table of attributes (name, type, required?,
     description, constraints) — described, **no `CREATE TABLE` code** — grouped
     under the four databases above.
  4. Keys, relationships, cardinality, referential rules. **Cross-database
     references are by id only** (no hard FKs spanning cfg/data/logs/stats).
  5. Data lifecycle per database: creation, retention, archival, deletion (logs
     and stats carry their own retention/rollup policy).
  6. Data volume & growth estimates; indexing/partitioning *strategy* (not SQL).
  7. Data quality, validation, and privacy classification of fields (PII?).
- **Production-grade bar:** every entity in the domain model appears and is
  **assigned to exactly one of cfg/data/logs/stats with a stated reason**; cross-DB
  references are id-only; attributes have types and constraints; PII/sensitive
  fields are flagged; volume estimates ground storage/scaling decisions.

### 06 — Interface / API Design
- **Purpose:** the contracts between this system and its clients/other systems —
  **described contracts, not implementation.** The system's own API surface is
  **unified as REST** (item 1).
- **Upstream:** 01, 03, 04 (feature tree), 05 (data).
- **Structure:**
  1. **REST by default — the unified style.** The API surface is RESTful:
     **resource-oriented URLs with plural nouns** (`/leads`,
     `/opportunities/{id}/follow-ups`), **standard HTTP methods** with correct
     semantics (GET read · POST create · PUT replace · PATCH partial-update ·
     DELETE remove) and **correct status codes** (201 create, 202 async-accepted,
     204 empty, 4xx/5xx), **`/api/v1` versioning**, and consistent
     pagination / filtering / sorting conventions. Non-CRUD actions use a
     sub-resource or a clearly-named verb path (`POST /opportunities/{id}/close`).
     Deviate from REST only with a **stated reason** (e.g. a streaming/RPC need);
     event/queue interfaces belong in the async doc (07), not here.
  2. **API inventory — derived page-by-page.** First build a **Page → interface
     map**: walk each page/screen in the feature tree and list what it **displays**
     (the data it reads — lists, detail, metrics, dictionary/lookup) → the
     **read/query endpoints** it needs, and the **operations** it offers
     (create / edit / delete / state-change / export…) → the **action endpoints** —
     as a table of *page · display-or-operation · type (display / action) ·
     endpoint(s) · role*. Then roll those up into the **full endpoint inventory**
     grouped by module/resource (**method · path · summary · module · auth · role**),
     reviewable at a glance. Deriving the surface from each page's real display +
     action needs — not only from the feature tree's operations — is what makes it
     **complete**: it catches the read / query / lookup endpoints an operation-only
     inventory misses. The inventory must line up with both the feature tree and the
     page→interface map.
  3. Per endpoint/operation: purpose, inputs, outputs, key fields, status/errors,
     idempotency & auth — as tables, **not code**.
  4. **OpenAPI / Swagger** — the API is specified as an **OpenAPI (Swagger)
     document** and **Swagger UI is enabled** as the live, browsable contract. This
     doc is the human design; the OpenAPI spec is its machine-readable source of
     truth for client/server. Note where the spec lives and that it's generated/
     kept in sync (e.g. NestJS `@nestjs/swagger`, springdoc, etc.).
  5. Common data contracts / shared schemas (reference the data model).
  6. Error model: standard error shape, error codes, retry semantics.
  7. Versioning & compatibility policy.
  8. Rate limits, pagination, filtering conventions.
  9. Integration contracts with external systems (SLAs, payloads, failure modes).
- **Production-grade bar:** the surface **follows REST conventions uniformly**
  (resources, verbs, status codes, versioning) with any deviation justified; field
  names and types match the data model exactly; a consistent error model covers
  the whole surface; auth and versioning are specified; an **OpenAPI spec is
  produced and Swagger UI enabled**; the inventory is **derived from the
  page→interface map** and matches the feature tree — every page's display data has
  a read/query endpoint and every operation has an action endpoint, and conversely
  no endpoint is an orphan.

### 07 — Async / Background Processing Design
- **Purpose:** how work that *isn't* in the synchronous request/response path gets
  executed — **job workers, async tasks, and scheduled (cron) tasks** — their
  triggers, execution model, reliability, and observability. The API doc (06)
  covers synchronous contracts; this covers everything that runs behind them or on
  a clock.
- **When to include:** whenever the system has any background work — async side
  effects (notifications, external-system sync, exports), queue/worker processing
  (imports, batch, heavy compute, fan-out), or scheduled jobs (rollups, reminders,
  retention/cleanup, timers). Skip only for a purely synchronous CRUD app (say so).
- **Upstream:** 03 (architecture/topology), 04 (feature tree — which operations
  spawn async work), 05 (data — job & log records).
- **Structure:**
  1. **Task inventory** — a table of every async task / job / scheduled task:
     name · type (async-event \| worker/queue \| scheduled/cron) · trigger
     (event / API call / cron expression) · purpose · input · frequency · **idempotency
     key** · **retry policy + backoff** · timeout · concurrency/rate limit · priority ·
     **failure handling (DLQ / alert)** · owner.
  2. **Categories & execution model:**
     - **Async tasks** — fire-and-forget side effects of an operation (enqueue on
       action, e.g. notify Hermes, send email); the API returns fast (202 + a
       status/result endpoint where the caller needs it).
     - **Job workers** — queue consumers / long-running / batch (imports, heavy
       compute, fan-out).
     - **Scheduled / cron tasks** — periodic (stats rollups, reminders,
       retention/cleanup, SOP timers, **active cache warming / refresh-ahead** —
       see the Caching Design doc).
  3. **Infrastructure** — queue/scheduler technology, **consistent with the
     tech-selection doc** (e.g. BullMQ+Redis for NestJS, Celery/Redis, Sidekiq,
     Spring `@Scheduled`/Quartz, k8s CronJob); worker topology (in-process vs a
     separate worker deployment) and how it maps to the deployment view.
  4. **Reliability semantics** — at-least-once vs exactly-once, idempotency,
     retries & backoff, dead-letter handling, ordering guarantees, **a distributed
     lock / leader election so scheduled jobs don't double-run under HA / multiple
     instances**, and backpressure.
  5. **Triggers map** — which feature-tree operations / domain events spawn which
     tasks (traceability), and which task writes where (→ logs / stats DB).
  6. **Observability & ops** — job logging (→ logs DB), metrics (queue depth,
     success/fail, latency), alerting, a retry/replay runbook, and job-record
     retention.
- **Production-grade bar:** every async/scheduled task is in the inventory with a
  **trigger, idempotency stance, retry/failure policy, and where it's observed**;
  scheduled jobs are **safe under multiple instances** (no double-run); the
  queue/scheduler tech is named and matches tech-selection; async side effects
  trace back to the feature tree; nothing is "fire and pray."

### 08 — Non-Functional Requirements (NFRs)
- **Purpose:** the quality attributes, each with a **measurable target**.
- **Upstream:** 01; informs 03.
- **Structure (only the categories that apply):** performance & latency;
  scalability & capacity; availability & reliability (SLA/SLO, RTO/RPO);
  security (authn/z, data protection, threat surface); privacy & compliance;
  observability (logging, metrics, tracing, alerting); maintainability &
  operability; usability / accessibility / **responsive** (target devices &
  breakpoints); **internationalization** (default bilingual: user-language +
  English, on an extensible i18n foundation — no hard-coded strings); **theming**
  (light / dark / follow-system); portability; cost targets.
- **Production-grade bar:** every NFR is *quantified and verifiable* ("p95 API
  latency < 300 ms at 1k RPS"), not aspirational ("must be fast"). Each ties to a
  business reason and, where relevant, a verification method.

### 09 — Technology Selection & Evaluation
- **Purpose:** justify the stack so choices are defensible.
- **Upstream:** 03, 07 (async infra), 08 (NFRs).
- **Structure:**
  1. Decision areas (language/runtime, framework, datastore, queue, hosting…)
  2. Per area: options considered, evaluation criteria (fit, maturity, team
     skill, cost, ecosystem, lock-in), a comparison matrix, the recommendation +
     rationale, and risks/mitigations
  3. Overall stack summary & key trade-offs accepted
- **Production-grade bar:** at least two real alternatives per significant
  decision; criteria are explicit and applied consistently; recommendations note
  what is being traded away, not only the upside.
- **Decide the headline stack early.** The front-end framework + UI library,
  back-end framework, **database(s)**, and architecture shape should already be
  settled in Phase 0 (they drive the prototype, the data design, and the project
  structure). This document *justifies* those choices formally and decides the
  finer-grained ones (queue/broker, cache, search, hosting, etc.) — it doesn't
  discover the headline stack for the first time.

### 10 — Project Structure & Directory Design
- **Purpose:** define the codebase skeleton — the directory/module layout the team
  starts from. It's a genuine design artifact because it's **dictated by the
  chosen framework and architecture shape**, and getting it right prevents months
  of inconsistent organization.
- **Upstream:** 09 (framework choice), 03 (architecture — module boundaries).
- **Structure:**
  1. **Module layer — required for a large sub-system, on BOTH stacks.** Organize
     *both* front-end and back-end **by module/feature first**, not only by type:
     a `modules/<module>/` layer where each module is **self-contained** and maps
     to a feature-tree module / architecture boundary. A small sub-system may stay
     type-first (flat `views/` + `components/`); state which and why. The module
     layer is what keeps a large codebase navigable and lets modules be split out
     later.
  2. Front-end directory tree for the chosen framework/UI library. **Large:**
     `src/modules/<module>/{views, components, composables, api, store}` (module-
     scoped) + top-level `shared/` (cross-module components/utils), `router/`,
     `app store`, `styles/tokens`, **i18n catalog (locales)**, **theme/dark-mode
     handling**, `assets`. **Data-access convention:** a shared **`apiClient`** (base
     HTTP — interceptors, a **unified auth/token layer** (attach `Authorization:
     Bearer`, refresh / redirect on 401), error envelope; the single config/swap
     point), **one api module per resource**, composed into an **aggregate `api`** so
     call sites
     read `api.<module>.<method>(args)` (each method ↔ a doc-06 endpoint); components
     never call `fetch`/`axios` directly. **Small:** flat `views/` + `components/`.
     Real folder names — e.g. Vite + Vue 3 + Element Plus, Next.js app router, or
     CRA/Vite + shadcn.
  3. Back-end directory/package layout for the chosen framework **and architecture
     shape** — **module-first**: `src/modules/<module>/{controller, service, dto,
     entity}` (e.g. NestJS one module per domain / Spring Boot package-by-feature /
     per-service repos for microservices); plus `integration/` adapters, `common/`,
     `config/`, and where **job workers / queue consumers / scheduled tasks** live.
  4. Cross-cutting: config & env, shared libs, where API contracts / shared types
     live (and how front-end and back-end share them).
  5. Module boundaries & dependency rules (what may import what; **modules don't
     reach into each other's internals** — only via public service/API or events)
     — these encode the architecture shape (modular-monolith boundaries vs splits).
  6. Conventions: file/naming conventions, mono-repo vs multi-repo decision.
- **Production-grade bar:** the trees are concrete for the *actual* chosen stack
  (real folder names, not abstract placeholders); **a large sub-system shows a
  module layer on both front-end and back-end**, with modules self-contained and a
  clear shared/common separation; the layout aligns with the architecture's module
  boundaries; a new engineer could drop a new feature into the right module without
  asking; dependency rules are explicit.

### 11 — Risk Assessment & Mitigation
- **Purpose:** surface what could go wrong while it's still cheap to address.
- **Upstream:** all prior docs.
- **Structure:**
  1. Risk register table: ID, description, category (technical/schedule/
     security/operational/external), likelihood, impact, exposure, mitigation,
     contingency, owner
  2. Top risks called out with a mitigation plan
  3. Key assumptions whose failure is itself a risk
- **Production-grade bar:** risks are specific and scored consistently; each has a
  concrete mitigation and an owner; assumptions from Phase 0 that could break the
  design appear here.

### 12 — Test Strategy
- **Purpose:** how quality will be verified — strategy, **not test cases/code.**
- **Upstream:** 01, 08.
- **Structure:**
  1. Test levels & scope (unit, integration, system, E2E, performance, security,
     UAT) and what each covers
  2. Approach per level, environments & test data strategy
  3. Quality gates & entry/exit criteria; definition of done
  4. Requirements traceability: each FR/NFR → how it's verified
  5. Tooling categories, automation strategy, and risks to test coverage
- **Production-grade bar:** every functional and non-functional requirement maps
  to at least one verification approach; gates are objective; performance and
  security testing are addressed when the NFRs demand them.

### 13 — Roadmap & Milestones
- **Purpose:** sequence the work into deliverable phases.
- **Upstream:** all prior docs.
- **Structure:**
  1. Delivery phases / milestones with goals and exit criteria
  2. Scope per phase (MVP first), mapped to requirement IDs
  3. Dependencies & critical path; key sequencing constraints
  4. Rough effort/sizing (relative or T-shirt — flag as estimate)
  5. Assumptions affecting the timeline
- **Production-grade bar:** phases deliver coherent, demonstrable value; an MVP is
  identifiable; sequencing respects technical dependencies; estimates are clearly
  labeled as estimates with their assumptions.

### 14 — Task Plan & Delivery Workflow
- **Purpose:** turn the design into an **executable, incrementally-verifiable
  backlog** — sliced tasks with acceptance criteria, plus the closed-loop
  "definition of done" the build team/agent follows. The bridge from design to
  build: the roadmap (13) gives phases; this gives the verifiable **task slices**
  and the process for each. Slices are **derived from the Feature Tree (04)** and
  ordered by the roadmap.
- **When to include:** whenever the design will be handed to a team or an AI agent
  to build (i.e. almost always for a project meant to be implemented).
- **Upstream:** 04 (feature tree — the slice source), 06/07 (API & async per
  slice), 13 (roadmap — phase ordering), and the repo's style Skill + Rubric D for
  UI pre-flight.
- **Structure:**
  1. **Slicing rules.** Each task is **independently acceptable and testable** —
     never swallow a whole module/system in one task. A **large task** (changes
     >5 files, or spans ≥2 dirs/stacks, or reads as "entire module / all APIs /
     whole page / migration+API+front-end", or can't be tested-and-reported in one
     vertical slice) must **first be broken into a subtask checklist**
     (`- [ ] subtask` — one per line). Granularity by type: **API** = one resource
     or a symmetric endpoint group; **page** = one user path; **migration** = a
     batch of related tables (its API in the same or the next slice).
  2. **Task cards** — one per slice / subtask:
     ```
     【Name】【Parent / seq】【Doc anchor: FR-… / feature-tree node】【Scope: in / out】
     【Done criteria】【Test cases】【Test command】
     【UI verify: URL + steps, or "none"】【UI style: repo style Skill + UI pre-flight (Rubric D)】
     ```
  3. **Gating (serial delivery).** subtask 1 **all green** → subtask 2 → … → final
     report. Read-only investigation may run in parallel; **delivery is serial**;
     each subtask runs the full closed loop; **no next subtask until the current is
     green** (tick progress in the report).
  4. **Closed loop / DoD per task** (repeat ①–⑨ per subtask; small task = one pass):
     ① **Read** docs + existing code, note frontmatter version · ② **Plan** —
     no doc anchor → document it first; large task → emit subtask checklist; fork/
     over-design → ask the user; keep changed files ≤5 · ③ **Implement** to the
     live docs + module conventions, minimal diff · ④ **Test** — find the test
     method, run the cases + command; UI → browser check + style Skill + UI
     pre-flight · ⑤ **Triage** failures (impl / test / doc) — no skipping, no
     commented-out assertions, no "known failure" · ⑥ **Fix** — restart/boot
     dependencies if needed, re-run the *same* command to green; **don't push
     verification to the user** · ⑦ **SelfCheck** · ⑧ **Sync** — new/changed
     requirements → update the live docs + bump version · ⑨ **Report** — per-
     subtask brief, then an overall report when all done.
  5. **Failure triage:** implementation bug → fix code; test bug → fix test/config;
     doc/case bug → fix the doc or task card first, then code. **Never mark done
     without re-running tests green.**
  6. **Agent defaults:** don't commit/push unless the user asks; don't expand
     scope; minimal diff.
- **Production-grade bar:** every slice traces to a feature-tree node / FR and has
  **done-criteria + a test command + (if UI) a verify path**; large tasks are
  pre-decomposed into checklists; the DoD is explicit and **self-verifying** (the
  builder runs and confirms — it doesn't ask the user to test).

---

## Conditional documents

Include when the condition applies. Same structure discipline as above.

- **UX / Interaction Design Brief** — *when* the product is user-facing with
  non-trivial UI, and **always when the Prototype phase is in scope** (it's the
  spec the prototype is built from). Key flows, wireframe-level screen inventory
  (described), interaction & state rules (incl. the **empty / loading / error**
  states and **form presentation** — modal dialog / drawer for create/edit, a
  full-page or wizard only for genuinely complex multi-step input), **navigation map
  / route tree** (top-level + **nested / child routes** — master→detail, tabbed
  sub-sections — each mapping to a feature-tree page),
  accessibility, **responsive breakpoints** (the target
  devices and the breakpoint set — e.g. mobile ≤640, tablet 641–1024, desktop
  ≥1025 — plus how layout/nav reflows at each), **theme modes** (light / dark /
  follow-system, and where the toggle lives), **language switching** (default
  user-language + English, with a placement for the switch), and the **visual
  direction** to hand to the prototype generator. The visual direction must state,
  **per surface, its usage-scenario type (admin console / utility tool / marketing
  site / data dashboard / mobile app…) and its UI-aesthetic bar** (settled in
  Phase 0) — because that decides not only the mood/palette but the **functional
  layout**: information density, navigation pattern, and how much screen goes to
  chrome vs content differ sharply between an efficiency-first internal console and
  a brand-forward marketing site. Name the **icon library** to use (the UI library's
  own set, or a chosen open library — Iconify / Lucide / Tabler / Heroicons). Fold
  in any **reference material the user provided** (mockups / 效果图, competitor
  screenshots, brand or style guide) and the mood/brand constraints. Described in
  words/ASCII — no visual design assets in
  this doc; the pixels live in the prototype. The breakpoints defined here are what
  the prototype's CSS must actually implement.
- **Security Design** (dedicated) — *when* handling sensitive data, payments,
  auth-heavy, or regulated. Threat model (e.g. STRIDE), trust boundaries, data
  protection at rest/in transit, secrets/key management, authz model, audit.
- **Privacy & Compliance** — *when* PII/health/financial data or a named regime
  (GDPR, 等保, HIPAA, PCI-DSS, SOC 2…). Data inventory & classification, lawful
  basis/consent, retention, data-subject rights, residency, control mapping.
- **Integration / Migration Plan** — *when* replacing a system or integrating
  with significant existing systems. Source→target mapping, migration approach
  (big-bang/phased), cutover, rollback, data reconciliation.
- **Operations / Deployment & Observability Plan** — *when* the team owns
  running it in production. CI/CD strategy, environments, release & rollback,
  monitoring/alerting, runbooks scope, backup/DR.
- **Cost / Capacity Estimate** — *when* cost is a stated constraint or scale is
  large. Cost drivers, capacity model tied to NFR volumes, scenarios.
- **Caching Design (缓存设计 — 关键点缓存)** — *when* the system has
  performance-sensitive reads, hot lookups, or a cache layer (e.g. Redis chosen) —
  i.e. most non-trivial back-ends. Covers:
  1. **Cache key-points (缓存关键点)** — identify what's worth caching: high-read /
     low-write, expensive-to-compute or to-fetch, hot lookups (dedup checks, RAG /
     playbook lookups, config & dictionaries, aggregates/leaderboards). Each point
     weighs **hit-rate benefit vs staleness risk**.
  2. **Passive caching (被动缓存 · cache-aside / read-through)** — populate **on
     miss**, lazy, with TTL. Include the three guards: **击穿**(stampede →
     mutex / single-flight), **穿透**(miss-storm → negative/empty caching or
     bloom filter), **雪崩**(synchronized expiry → TTL jitter).
  3. **Active caching (主动缓存 · write-through / 预热 / refresh-ahead)** — keep the
     cache hot ahead of demand: **write-through / write-behind** on updates,
     **cache warming / precompute** at startup or on a schedule (a scheduled task
     in the **async doc 07**), and **refresh-ahead** (refresh just before expiry).
     For data that must always be fresh or always hit (active config, SOP rules,
     hot playbooks, precomputed stats).
  4. **Invalidation & consistency** — TTL expiry vs **event-driven invalidation**
     (publish invalidate/update on write); **versioned keys**; per-key **staleness
     tolerance** (strong → active + event-invalidation; weak → passive + TTL).
  5. **Key design & layering** — key namespacing, layers (in-process → Redis →
     CDN/edge), eviction policy (LRU/LFU), memory budget.
  6. **Cache-point table** — *key · mode(被动/主动) · source · TTL/refresh ·
     invalidation trigger · staleness tolerance · hit-rate target*.
  - **Bar:** every cache-point states mode + source + invalidation + staleness
    tolerance + hit target; **active** points have a matching warm/refresh task in
    doc 07; invalidation is wired to the write path (no stale-read point missed);
    stampede/penetration/avalanche each have a countermeasure. Consistent with the
    cache/queue tech in [09] and the latency targets in NFR.

---

## Prototype (Phase 4 artifact — not a document)

The prototype is the one **non-document** deliverable: a runnable interactive
prototype (a real project in the chosen framework, or static HTML for a quick
concept), produced in **Phase 4** of the pipeline (see SKILL.md), not in the
per-document loop. It belongs here only so the plan accounts for it.

- **Purpose:** make the design clickable so UX and scope problems surface before
  any code is committed.
- **Upstream:** UX / Interaction Design Brief (screens & flows), Data Design
  (realistic fields/content), Requirements (the journeys worth prototyping).
- **How it's built:** to **production standards** in the **actual chosen front-end
  stack** (e.g. Vite + Vue 3 + Element Plus, or React + Ant Design) using its **real
  components**, structured as **pages + reusable components** (not one giant file)
  with a **data-access layer** — an **`apiClient`** (base HTTP, **unified auth/token**
  (attach Bearer, handle 401) + the swap point), **per-module api modules**, and an
  **aggregate `api`** called as
  `api.<module>.<method>(args)` — **behind which the mock data sits as a swappable
  source** shaped like the page→interface map (doc 06) endpoints, so real development
  is **switching the source for the live API**, not a rewrite — and
  **working interactions** (**create/edit forms in modal dialogs / drawers**,
  confirm dialogs, toasts, drill-down, empty/loading/error) and **icons from the
  chosen icon library** (the UI library's own set, or an open library like
  Iconify / Lucide when the framework ships none). `frontend-design` informs the
  visual direction; the build uses the real library. Scoped to MVP / key flows;
  each screen annotated with its FR IDs.
  Runs via `install + dev server` (state the command), **and ships a compiled
  preview** (`npm run build` → `dist/`, configured to open statically — Vite
  `base: './'` + hash routing) so reviewers can view the built HTML without the
  toolchain. Static single-file HTML is the fallback only when no stack was chosen.
- **How it's reviewed:** the **taste** review — driven by the `taste` skill
  (`design-taste-frontend`, install `npx skills add Leonxlnx/taste-skill`), with
  Rubric D in `references/review-rubrics.md` as the manual fallback — judges
  visual style; a quick functional check confirms flow/field coverage. Findings
  and any doc feedback go in `prototype-notes.md`.
- **Production-grade bar:** looks like a deliberate design for *this* product (not
  a templated AI default), covers the key flows end-to-end with realistic
  content, meets the quality floor (responsive, keyboard focus, reduced motion),
  and stays consistent with the requirements/data model. **Built to production
  standards** — data behind a swappable source keyed to the documented endpoints, so
  the path to real is *switch the source*, not rewrite. **Scope discipline:** that's
  production *standard*, not production *scope* — it stays a front-end, mock-data,
  MVP-scoped validation artifact; the apiClient **auth/token plumbing (mock token) is
  expected**, but don't gold-plate it with a backend / a real auth-identity provider /
  extra features, and don't let its implementation choices leak into the design
  documents.

## Production-grade bar — general

Apply to every document, on top of its specific bar:

- **Versioned.** Open with frontmatter — `doc_version`, `updated` (absolute date),
  `status` (draft/reviewed/approved) — and bump it on each material change (see
  SKILL.md *Iteration & version management*).
- **Concrete over vague.** Numbers, names, and criteria — not adjectives.
- **No empty templates.** Every section has real content or an explicit, reasoned
  "N/A". A heading with nothing under it is a defect.
- **Self-consistent and cross-consistent.** Uses the project's ubiquitous
  language; reuses (never contradicts) decisions and names from upstream docs.
- **Traceable.** Downstream content references the requirement/decision it serves.
- **Honest about uncertainty.** Unknowns are assumptions or open questions, never
  invented facts.
- **Skimmable.** Tables and diagrams (Mermaid/ASCII) where they beat prose; short
  sections with clear headings.
