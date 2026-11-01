from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import yaml
import jsonschema

from .common import files


PROJECT_MARKERS = (
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile",
    "setup.cfg", "PROJECT-STANDARDS.md", ".skill-refiner/standards",
)

HOST_SUBAGENT_TEST_RULES = (
    "When the orchestration host exposes subagent scheduling, prefer host subagents for test execution and create one bounded assignment per frozen development or acceptance case.",
    "Record the responsible Agent, case and requirement IDs, fixture/oracle references, expected outcome and assertion IDs, isolated scope, unique evidence path, limits, and join gate in the schema-validated execution_plan. A tester returns the case ID, PASS/FAIL/ERROR/NOT_RUN, each assertion's expected and actual result, evidence references, and gaps; it does not edit frozen plans/oracles, the target Skill, shared run state, or the parent GoalExecutor snapshot.",
    "The main controller owns result synthesis, repairs, phase gates, and shared progress. When large-task-tracking is active, mirror each case as a separate task and update it only from that case's evidence. Schedule case assignments serially by default; parallelize only with actual host support, separate fixtures/workspaces/evidence paths, and no shared runner or judge state. Same-executor skill-up cases remain serial while its script judge uses shared temporary directories.",
    "If host scheduling is unavailable or the executor only accepts a whole suite, state that exact fallback and preserve an independent status and evidence record for every case. The controller's max_parallel_agents is not proof that a host subagent ran.",
)


def _project_chain(start):
    """Return directories from the project boundary through start, root first."""
    path = Path(start).expanduser().resolve()
    if path.is_file():
        path = path.parent
    ancestors = []
    current = path
    git_root = None
    marker_root = None
    while True:
        ancestors.append(current)
        if (current / ".git").exists():
            git_root = current
            break
        if marker_root is None and any((current / marker).exists() for marker in PROJECT_MARKERS):
            marker_root = current
        if current.parent == current:
            break
        current = current.parent

    boundary = git_root or marker_root or path
    selected = []
    for directory in ancestors:
        selected.append(directory)
        if directory == boundary:
            break
    return list(reversed(selected))


def discover_project_standards(target=None, explicit=(), working_dir=None):
    """Discover conventional project standards and append explicit additions.

    Discovery walks the current project and target project independently. In a Git
    checkout it is bounded by the repository root; outside Git it is bounded by the
    nearest conventional project marker, or the starting directory itself.
    """
    bases = [Path(working_dir or Path.cwd()).expanduser().resolve()]
    if target is not None:
        bases.append(Path(target).expanduser().resolve())
    directories = []
    seen_directories = set()
    for base in bases:
        for directory in _project_chain(base):
            if directory not in seen_directories:
                directories.append(directory)
                seen_directories.add(directory)

    discovered = []
    seen_paths = set()
    for directory in directories:
        candidates = [directory / "PROJECT-STANDARDS.md"]
        standard_dir = directory / ".skill-refiner" / "standards"
        if standard_dir.is_dir():
            candidates.extend(sorted(standard_dir.glob("*.md")))
        for candidate in candidates:
            if candidate.is_file():
                resolved = candidate.resolve()
                if resolved not in seen_paths:
                    discovered.append(str(resolved))
                    seen_paths.add(resolved)
    for candidate in explicit or ():
        resolved = Path(candidate).expanduser().resolve()
        if resolved not in seen_paths:
            discovered.append(str(resolved))
            seen_paths.add(resolved)
    return discovered


AUTHORING = """Maintain the user's scope and public input/output contract. Make trigger descriptions
specific. Keep the entrypoint focused and put substantial conditional details in discoverable
references. Explain important decisions; avoid arbitrary mandatory headings, length padding,
or adding examples/configuration where irrelevant. Prefer narrow fixes supported by failures.
Use reusable scripts for demonstrated repeated deterministic work. Do not delete capabilities
or weaken existing tests. Normative requirements come from supplied requirements and standards;
the current implementation is evidence, not an infallible source of truth. When creating a new
Skill, default all user-facing instructions and human-readable package text to Simplified Chinese,
including SKILL.md, references, examples and script messages. Preserve code identifiers, schema
property names, protocol literals, source quotations and the workflow's required business-output
language; honor an explicit user language request. When refining an existing Skill, preserve its
established language unless the user requests translation. Treat the chosen language as an
acceptance requirement and include representative language checks in the frozen test plan. For Skills that can
be blocked by a material business decision, local configuration, permissions, or an unavailable
dependency, include a conditional help-request path: inspect evidence first, explain the concrete
blocker, ask only for the smallest safe choice or action, and continue with fresh checks after
the answer. Use a host-native input tool when available and conversation otherwise. Never request
secrets in chat. Do not add unnecessary questions to deterministic workflows."""
STAGE_ARTIFACT_STANDARD = (Path(__file__).resolve().parents[2] / "references" /
                           "skill-stage-artifact-standard.md").read_text(encoding="utf-8")
SELF_REVIEW_STANDARD = (Path(__file__).resolve().parents[2] / "references" /
                        "self-review-protocol.md").read_text(encoding="utf-8")
AUTHORING += "\n\n" + STAGE_ARTIFACT_STANDARD
AUTHORING += "\n\n" + SELF_REVIEW_STANDARD


def lint(root, expected_name=None):
    root = Path(root)
    findings = []

    def add(rule, path, message, severity="error"):
        findings.append({"id": f"S-{len(findings)+1:03}", "rule": rule,
                         "path": path, "message": message, "severity": severity})

    entry = root / "SKILL.md"
    if not entry.exists():
        add("format.entrypoint", "SKILL.md", "Missing SKILL.md")
        return findings
    text = entry.read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    front = {}
    if not match:
        add("format.frontmatter", "SKILL.md", "Missing YAML frontmatter")
    else:
        try:
            front = yaml.safe_load(match[1])
            if not isinstance(front, dict):
                raise ValueError("Frontmatter must be a mapping")
        except (yaml.YAMLError, ValueError) as exc:
            add("format.frontmatter", "SKILL.md", str(exc))
            front = {}
    name = front.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        add("format.name", "SKILL.md", "Name must be nonempty kebab-case, at most 64 characters")
    elif expected_name and name != expected_name:
        add("contract.identity", "SKILL.md", f"Preserve skill name {expected_name!r}")
    for key, limit, required in [("description", 1024, True), ("compatibility", 500, False)]:
        value = front.get(key)
        if required or value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                add(f"format.{key}", "SKILL.md", f"{key} must be a nonempty string of at most {limit} characters")
    if "metadata" in front and (not isinstance(front["metadata"], dict) or
                               any(not isinstance(v, str) for v in front["metadata"].values())):
        add("format.metadata", "SKILL.md", "metadata must map string keys to string values")
    schema_root = root / "schemas"
    schema_files = sorted(schema_root.rglob("*.schema.json")) if schema_root.is_dir() else []
    if not schema_files:
        add("contract.schemas", "schemas", "Skill must package at least one versioned JSON Schema")
    for schema_path in schema_files:
        relative = str(schema_path.relative_to(root))
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            if (not isinstance(schema, dict) or not isinstance(schema.get("$id"), str)
                    or not schema["$id"].strip()
                    or not isinstance(schema.get("x-schema-version"), str)
                    or not re.fullmatch(r"v[0-9]+", schema["x-schema-version"])):
                raise ValueError("Schema must be a JSON Schema object with a stable $id and x-schema-version")
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, jsonschema.SchemaError) as exc:
            add("contract.schema.invalid", relative, str(exc))
    if len(text.splitlines()) > 500:
        add("authoring.entrypoint", "SKILL.md", "Consider moving conditional detail to references", "warning")
    for p in files(root):
        rel = str(p.relative_to(root))
        if p.suffix == ".py":
            try:
                ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeError) as exc:
                add("script.syntax", rel, str(exc))
        if p.suffix == ".md":
            content = p.read_text(encoding="utf-8")
            for ref in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", content):
                if "://" not in ref and not ref.startswith(("/", "#", "mailto:")) and not (p.parent / ref).exists():
                    add("resource.reference", rel, f"Broken relative reference: {ref}")
    return findings

