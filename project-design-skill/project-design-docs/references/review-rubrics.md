# Review Rubrics

The checklists that make the create → **review** → optimize loop real. Read this
in **Phase 3b** (per-document content + logic review), **Phase 4b** (the taste /
visual-style review of the prototype), and **Phase 5** (cross-document
consistency review).

The point of a review pass is to find problems, not to confirm the draft is
fine. Read as a skeptic who assumes there *are* issues and goes looking for
them. A review that surfaces nothing on a first draft almost always means the
review was too shallow — push harder before accepting that.

Contents:
- [Findings format & severity](#findings-format--severity)
- [Skill review checklist (master index)](#skill-review-checklist-master-index)
- [Rubric A — Content review](#rubric-a--content-review-per-document)
- [Rubric B — Logic review](#rubric-b--logic-review-per-document)
- [Rubric C — Consistency review](#rubric-c--consistency-review-cross-document)
- [Rubric D — Taste / visual-style review](#rubric-d--taste--visual-style-review-prototype)
- [Independent review](#independent-review)

---

## Findings format & severity

Record each finding as:

```
[SEVERITY] <location: doc / section> — <what's wrong> → <concrete fix>
```

- **Blocker** — would mislead the build or invalidate a decision; must be fixed
  before the document is `optimized`. (e.g. a requirement with no acceptance
  criteria; an API field that doesn't exist in the data model; an NFR that
  contradicts the chosen architecture.)
- **Major** — materially weakens the document; fix unless there's a stated
  reason not to. (e.g. a tech choice with no alternatives considered; a vague,
  unmeasurable NFR.)
- **Minor** — clarity/polish; fix if cheap. (e.g. inconsistent term casing, a
  missing example.)

A document is `optimized` only when all **blocker** and **major** findings are
actually resolved — not merely acknowledged. After applying fixes, re-check the
specific items rather than assuming the edit worked.

---

## Skill review checklist (master index)

**The review mechanism: every review pass is checklist-driven, not freeform.** Build
the checklist for the artifact under review from the relevant section below, **tick
each item pass / fail / N-A with evidence** (file + location, or the click that proved
it), and **record the ticked checklist** in the review trail — `00-PROGRESS.md` per
document, `prototype-notes.md` for the prototype, `consistency-report.md` for the set.
Each failed item becomes a finding in the format above. **Status never advances while
any blocker/major box is unchecked.** The rubrics below are the detail behind each
line; this index is what you *walk* so nothing is skipped.

### Per-document review — Phase 3b (run for every document)
- [ ] Every catalog-template section present, real content, no TODO/placeholder
- [ ] That document's **production-grade bar** (its `document-catalog.md` entry) is met
- [ ] **Content** (Rubric A): complete · correct · specific/measurable · actionable ·
      right altitude · justified · no invention
- [ ] **Logic** (Rubric B): internally coherent · feasible · sound cause→effect ·
      goal-aligned · assumptions explicit · edge cases · upstream deps current
- [ ] **Version frontmatter** present and bumped (`doc_version` / `updated` / `status`)

### Prototype review — Phase 4b (Rubric D)
- [ ] Not a templated default; grounded in the subject; **calibrated to the surface's
      aesthetic bar**; honours any reference material
- [ ] Typography · structure-encodes-meaning · one signature + restraint · intentional
      copy · **quality floor** (visible focus, reduced-motion, consistent spacing)
- [ ] **Responsive** at the declared breakpoints (reflows, not just shrinks)
- [ ] **Cross-cutting defaults**: theme (light/dark/auto) legible; language switch flips
      strings via the i18n catalog (no hard-coded text)
- [ ] **Matches the chosen UI library**; **icons** from one library, used purposefully
- [ ] **Forms** in the library's dialog/drawer pattern (full-page/wizard only for complex)
- [ ] **Built to production standards**: `apiClient` + per-module modules + aggregate
      `api`; components call `api.<module>.<method>(args)` (no raw fetch/axios); **mock
      behind a swappable source** shaped like the doc-06 endpoints; **auth/token**
      (token interceptor + 401 handler) in `apiClient` with a mock token
- [ ] **Routing & structural completeness — no dead ends**: route tree incl. nested
      routes, every route → a real page; every route / sub-route / tab / button /
      state-toggle reaches a working implementation
- [ ] **Sub-system coverage**: every in-scope surface prototyped or explicitly deferred
- [ ] **Interaction click-verified**: route → page → element checklist exercised with
      preview/browser tools (evidence, not assertion)
- [ ] Consistent with requirements / UX brief / data model

### Consistency review — Phase 5 (Rubric C)
- [ ] **Terminology** one-name-everywhere; glossary obeyed
- [ ] **Entity/attribute alignment** domain ↔ data ↔ API ↔ requirements
- [ ] **Scope alignment**; **decision coherence** (arch / tech / NFR / data agree)
- [ ] **Feature-tree coverage**: each page's display + operations → read/action
      endpoints; the **page→interface map** is complete; prototype routes/screens ↔ tree
- [ ] **Roles** consistent; **REST** uniform; **cfg/data/logs/stats** partitioning;
      **module alignment**; **async coverage**; **cache consistency** (if a cache exists)
- [ ] **Traceability**: forward (FR → design → test) and backward; IDs unique & stable

### Every review pass
- [ ] Findings recorded with severity in the format above
- [ ] All **blocker/major** items fixed and **re-checked** (not merely acknowledged)
- [ ] The ticked checklist + the fixes appended to the review trail (auditable)

---

## Rubric A — Content review (per document)

Does the document say enough, correctly, to be acted on?

- **Completeness** — every section from the catalog template is present and has
  real content (or an explicit, reasoned "N/A"). No placeholders, no TODOs left.
- **Correctness** — claims are technically sound; nothing is factually wrong or
  internally impossible.
- **Specificity** — concrete and measurable where it should be (metrics, types,
  limits, criteria). Flag every "fast / scalable / secure / user-friendly" that
  isn't quantified.
- **Actionability** — could a competent team act on this without guessing? List
  what they'd still have to ask.
- **Right altitude** — neither hand-wavy nor drowning in premature detail for
  this project's scale.
- **Evidence & justification** — significant decisions state *why*; alternatives
  are acknowledged where relevant.
- **No invention** — anything not given or derivable is marked as an assumption
  or open question, not asserted as fact.

## Rubric B — Logic review (per document)

Does the document hold together and make sense?

- **Internal coherence** — no part contradicts another part of the same document.
- **Feasibility** — what's proposed is actually buildable under the stated
  constraints (scale, budget, deadline, stack).
- **Cause→effect soundness** — stated rationales genuinely support their
  conclusions; no non-sequiturs.
- **Goal alignment** — the content serves the requirements/goals it claims to;
  nothing is gold-plating outside scope, nothing in-scope is silently dropped.
- **Assumption integrity** — assumptions are explicit, plausible, and their
  "impact if wrong" is understood.
- **Edge cases & failure modes** — error paths, limits, and what-happens-when
  considered, not just the happy path.
- **Dependency sanity** — this document's reliance on upstream docs is correct
  and current (it didn't build on a since-changed decision).

## Rubric C — Consistency review (cross-document)

Run after all documents are individually optimized. This catches the failures
that per-document review structurally cannot.

- **Terminology** — one concept, one name, everywhere; the glossary covers key
  terms and the docs obey it. List every synonym/variant found.
- **Entity & attribute alignment** — entities, attributes, types, and
  relationships match across domain analysis ↔ data design ↔ API design ↔
  requirements. Flag any field referenced in an API that's absent from the data
  model, and vice versa.
- **Scope alignment** — nothing is in-scope in one document and out-of-scope (or
  absent) in another; the requirement set, design, tests, and roadmap describe
  the *same* product.
- **Decision coherence** — architecture, tech selection, NFRs, and data design
  don't contradict (e.g. consistency model, sync vs async, datastore choice vs
  data shape, security posture vs deployment).
- **Feature-tree coverage** — the **Feature Tree is the master checklist**: every
  leaf (display item or operation) has a role, an FR, an API endpoint, and (if
  prototyped) a screen; **each page's display/data maps to a read/query endpoint and
  each operation to an action endpoint** — walk every page's *display + operations*,
  not operations alone, since that's what surfaces missing read endpoints; conversely
  every prototype **route / screen (including nested routes)** and API endpoint
  appears in the tree. Flag any display lacking a read endpoint, any operation lacking
  an action endpoint, any route/screen not in the tree, and any tree page with no
  route.
- **Roles consistency** — the roles in requirements, the role column of the
  feature tree, and the auth/role on each API endpoint agree; no operation is
  reachable by a role the requirements don't grant it.
- **API style uniform (REST)** — every endpoint follows REST consistently
  (resource-oriented plural URLs, correct HTTP verb + status code, `/api/v1`
  versioning); flag verb-in-path CRUD (`/getLead`, `/createOpp`), inconsistent
  pluralization, or wrong status codes. Any non-REST endpoint must carry a stated
  reason; event/queue interfaces belong in the async doc, not the REST surface.
- **Database partitioning** — every entity is assigned to exactly one of
  cfg/data/logs/stats, and the API/feature-tree references no entity that the data
  design didn't place; cross-DB references are id-only.
- **Module alignment** — for a large sub-system, the **module layer** in the
  project-structure doc (front-end *and* back-end) matches the feature-tree modules
  and the architecture's module boundaries; no module exists in one doc but not the
  others, and module names agree.
- **Async coverage** — every async side effect, job, or scheduled task implied
  elsewhere (an operation that "notifies"/"syncs"/"exports", a 202 endpoint, a
  rollup/retention/reminder) appears in the async-processing doc with a trigger,
  idempotency, and retry/failure policy; scheduled jobs are HA-safe; job/event
  records land in the logs/stats DBs the data design defined.
- **Cache consistency** (if a Caching Design doc exists) — every cache-point has a
  mode (passive/active), an invalidation trigger wired to its write path, and a
  stated staleness tolerance; **active** points have a matching warm/refresh task
  in the async doc; no entity that's updated is left served stale without a reason.
- **Traceability — forward** — every functional requirement (FR) maps to a design
  element (architecture/API/data) **and** to a test-strategy verification. List
  orphan FRs.
- **Traceability — backward** — every design element and major component traces
  to a requirement or an explicit non-functional driver. List design that serves
  no stated need (possible gold-plating).
- **Numbering & references** — IDs (FR-/BR-/NFR-/risk) are unique and stable;
  cross-references between docs point to things that exist.

Produce `consistency-report.md`: the findings (with severity), a forward/backward
traceability table (requirement → design → test), and the list of fixes applied
to each affected document.

## Rubric D — Taste / visual-style review (prototype)

Run in **Phase 4b**, on the interactive prototype. This is the "taste" review: a
design critique, not a functional one. The bar is a good design lead's — *does
this look like a deliberate, distinctive design made for this specific product,
or like a templated default that could belong to anything?*

**Prefer the `taste` skill** (`design-taste-frontend`, https://www.tasteskill.dev/,
install: `npx skills add Leonxlnx/taste-skill`) to drive this review — it's a
purpose-built anti-slop frontend framework and its audit is more thorough than a
checklist. Use the criteria below when the taste skill can't be installed, or as
a quick sanity list alongside it. They mirror the same anti-slop principles
(shared with the `frontend-design` skill):

- **Not a templated default.** AI-generated UI clusters around a few looks (warm
  cream + serif + terracotta; near-black + one acid accent; hairline-rule
  broadsheet). If the prototype landed on one of these *without the brief asking
  for it*, that's a finding — name what's generic and what it should become.
- **Grounded in the subject.** The palette, imagery, and vocabulary come from
  this product's actual world and audience, not generic stock choices.
- **Calibrated to the surface's aesthetic bar.** Judge against the surface's
  declared usage-scenario type and UI-aesthetic bar (Phase 0). An efficiency-first
  internal console is held to *information density, clarity, and scannability*, not
  to marketing-site polish; a brand-forward public site is held to distinctiveness
  and craft. "Deliberate, not templated" applies to both — but don't ding a utility
  tool for being quiet, and don't pass a marketing page just because it's tidy. If
  the user supplied reference material, check the result honours its direction.
- **Typography has personality.** Display and body faces are paired deliberately
  with a real type scale; type is a designed element, not a neutral delivery of
  text.
- **Structure encodes meaning.** Devices like numbering, eyebrows, and dividers
  reflect something true about the content (a real sequence, a real hierarchy) —
  they're not decoration.
- **One signature, much restraint.** There is a single memorable element, and
  everything around it is quiet and disciplined. Flag scattered effects, gratuitous
  animation, and anything that could be removed without loss ("remove one
  accessory").
- **Copy is intentional.** Labels name what the user controls in plain, active
  language; the same action keeps its name through the flow; empty/error states
  give direction, not mood.
- **Quality floor.** Visible keyboard focus, reduced-motion respected, consistent
  spacing. Pass/fail, not taste.
- **Responsive to the defined breakpoints.** The prototype actually reflows at the
  breakpoints the UX brief defined (not just "shrinks") — nav, multi-column
  layouts, and tables behave sensibly at mobile/tablet/desktop. Flag layouts that
  break or overflow at a declared breakpoint.
- **Cross-cutting defaults present.** Unless excluded: theme switching
  (light / dark / follow-system) works and dark mode is actually legible (not just
  inverted), and a language switch flips strings via the i18n catalog (no
  hard-coded text left behind). Flag missing toggles or strings that don't switch.
- **Matches the chosen UI library.** If a front-end framework + UI library was
  settled (Phase 0), the prototype should read as *that* library — its accent,
  radius, control shapes, component patterns — not a generic theme. A prototype
  that ignores the picked stack is a finding, because it misrepresents what will
  be built.
- **Sub-system coverage.** Every in-scope sub-system that has a UI (admin console,
  public website, mobile app, operator workbench…) has its key screens prototyped,
  **or** is recorded as an explicit, reasoned deferral. Flag any sub-system that's
  silently missing — a prototype that only shows one surface of a multi-surface
  product is incomplete.
- **Forms use the library's dialog/drawer pattern.** Create/edit happens in a modal
  dialog or drawer (the library's idiom — `el-dialog`/`el-drawer`, antd `Modal`/
  `Drawer`), not an awkward full-page form for a simple record; a multi-step wizard
  is reserved for genuinely complex input. Flag a create/edit flow that navigates
  away to a bare page where a dialog/drawer is the expected pattern.
- **Icons from one library, used purposefully.** Icons come from the chosen icon
  library (the UI library's own set, or the agreed open library — Iconify / Lucide /
  Tabler / Heroicons), one consistent family at consistent sizes, used to aid
  scanning (nav, actions, status, empty states). Flag emoji or bare-text stand-ins
  for icons, clashing icon sets, or a UI left visually flat for want of icons — and
  conversely, icon clutter that decorates without informing.
- **Built to production standards — data behind a swappable source.** Data access
  goes through a layered `api` (an **`apiClient`** base + **per-module api modules** +
  an **aggregate `api`**), with components/views calling **`api.<module>.<method>(args)`**
  — never a raw `fetch`/`axios` or a mock fixture directly. The **mock is a swappable
  source behind `apiClient`**, shaped like the documented endpoints (doc 06). The
  acceptance test: could real development just point `apiClient` at the live API
  (call sites unchanged), or would it need a rewrite? The **auth/token mechanism sits
  in `apiClient`** too — a token-attaching interceptor and a **401 handler** (refresh
  / redirect to login), wired with a **mock token** in the prototype. Flag raw
  fetch/axios in components, inline mock data, a missing apiClient/aggregate layer,
  ad-hoc per-call auth or no token plumbing at all, or shapes that diverge from the
  page→interface map — each turns "switch the source" into "rebuild." (This is
  production *standard*, not *scope*: don't penalize the absence of a backend, a **real
  auth/identity provider**, or features beyond the key flows — those are correctly out
  of scope; the token *plumbing* with a mock token, though, is expected.)
- **Routing & structural completeness — no dead ends.** There's an explicit **route
  tree** (top-level + **nested / child routes**), and **every route resolves to a real
  implemented page** — no route 404s, no placeholder page, no page built but
  unrouted. Reviewed **structurally**, route → page → element: every **route,
  sub-route, tab, button, and state / status toggle reaches a working front-end
  implementation**, and each page's operations act on the **data structures the design
  defines** (feature tree + page→interface map + data model). Flag any dead link, dead
  tab, inert button, non-switching toggle, or route with no page — a control that
  renders but does nothing is a defect.
- **Interaction is click-verified (acceptance), not just present in code.** Every
  key interactive element was **actually exercised** and produced its expected
  effect: action buttons → dialogs/drawers, confirm → toast, row/card → drill-down/
  nav, tabs/filters/toggles → switch, theme & language toggles, and the
  empty/loading/error states reachable. Expect an interaction-acceptance checklist
  in `prototype-notes.md` with each element ticked; "it renders" is not acceptance.
  Call out any element that doesn't respond or whose effect is wrong.
- **Consistency with the design.** Screens, fields, and flows match the
  requirements, UX brief, and data model (this overlaps Rubric C — note anything
  here that needs a document change).

Record findings in `prototype-notes.md` using the same severity scale. A
**blocker** is "reads as a generic AI default" or a broken quality-floor item; a
**major** is a real-but-fixable taste weakness; **minor** is polish. The
prototype isn't `optimized` until blockers and majors are addressed — and "I
regenerated it and it still looks templated" means push harder on the brief, not
lower the bar.

---

## Independent review

For larger or higher-stakes projects, get a genuinely independent read by
spawning a subagent for Phase 3b (content/logic), Phase 4b (taste), and/or
Phase 5 (consistency) rather than reviewing your own work. Give the subagent: the
document(s) or prototype under review, this rubric file, and the relevant
upstream documents — and ask it to return only a findings list in the format
above, defaulting to flagging when uncertain. Independence catches the
blind spots an author re-reading their own work tends to miss. If subagents
aren't available, simulate the separation: set the draft aside mentally, re-read
top to bottom against the rubric as if someone else wrote it, and be willing to
find blockers.
