"""Run a frozen suite on every executor; never collapse cross-engine failures."""
from __future__ import annotations

import time
from pathlib import Path
import hashlib

from .common import Cancelled, RefinerError, digest, manifest, snapshot, write_json
from .profiles import skillup_engine
from .skillup import SkillUp


class Matrix:
    def __init__(self, profiles, work, skill_up=None, timeout=None, parallelism=2):
        self.profiles, self.work = profiles, Path(work)
        self.backends = [(p, SkillUp(skill_up, engine_config=skillup_engine(p, self.work / "profiles"),
                                    timeout=timeout, parallelism=parallelism)) for p in profiles]
        self.engine = {"matrix": profiles}
        self.evaluations = 0
        self.max_evaluations = 50
        self.deadline = None
        self.progress_callback = None
        self.case_progress_context = None

    def evaluate(self, target, cases, run_dir, *, selected=None, existing=True):
        started, rows, engines = time.monotonic(), [], []
        run_dir = Path(run_dir)
        chosen = [c for c in cases if selected is None or c["id"] in selected]
        if not chosen:
            raise RefinerError("Empty matrix case selection")
        frozen = digest(cases)
        for profile, backend in self.backends:
            engine_dir = run_dir / "engines" / profile["id"]
            engine_dir.mkdir(parents=True, exist_ok=True)
            isolated = engine_dir / "skill" / Path(target).name
            snapshot(target, isolated)
            if profile["backend"] != "skillup":
                write_json(self.work / "profiles" / (profile["id"] + ".json"),
                           {**profile, "_skill_manifest": {k: v for k, v in manifest(isolated).items()
                                                         if Path(k).parts[0] not in {"evals", "tests"}},
                            "_helper_manifest": {'refiner-python-probe.py': hashlib.sha256(
                                Path(__file__).with_name('python_probe.py').read_bytes()).hexdigest()}
                            if any('python_probe' in c.get('helpers',[]) for c in chosen) else {}})
            backend.max_evaluations = backend.evaluations + max(0, self.max_evaluations - self.evaluations)
            backend.deadline = self.deadline
            backend.probe_budget = getattr(self, 'probe_budget', None)
            before = backend.evaluations
            def relay_progress(elapsed, remaining, case_count, engine_label, child_evaluations):
                if self.progress_callback:
                    used = self.evaluations + max(0, child_evaluations - before)
                    self.progress_callback(elapsed, remaining, case_count, profile["id"], used)
            backend.progress_callback = relay_progress
            backend.case_progress_context = (
                {**self.case_progress_context, "executor": profile["id"]}
                if isinstance(self.case_progress_context, dict) else None)
            print(__import__("json").dumps({"stage": "engine", "executor": profile["id"], "cases": len(chosen)}), flush=True)
            result = None
            try:
                result = backend.evaluate(isolated, cases, engine_dir / "generated", selected=selected)
                if existing:
                    prior = backend.evaluate_existing(isolated, engine_dir / "existing")
                    if prior:
                        result["cases"].extend(prior["cases"])
                        result["passed"] = result["passed"] and prior["passed"]
            except Cancelled:
                raise
            except Exception as exc:
                # Preserve partial rows (including existing suite errors), and
                # continue with other engines so one outage never hides results.
                if result is None:
                    result = {"cases": [{"id": c["id"], "status": "ERROR", "passed": False,
                                         "error": str(exc)} for c in chosen], "passed": False}
                else:
                    result["cases"].append({"id": "existing:suite", "status": "ERROR", "passed": False, "error": str(exc)})
                    result["passed"] = False
                result["error"] = str(exc)
            finally:
                backend.case_progress_context = None
                self.evaluations += backend.evaluations - before
            result.update(executor=profile["id"], profile=profile, suite_sha256=frozen)
            write_json(engine_dir / "summary.json", result)
            engines.append(result)
            for row in result["cases"]:
                rows.append({**row, "id": profile["id"] + "::" + row["id"], "case_id": row["id"], "executor": profile["id"]})
        aggregate = {"passed": all(r["passed"] for r in rows), "cases": rows, "engines": engines,
                     "seconds": round(time.monotonic() - started, 3), "suite_sha256": frozen, "engine": self.engine}
        write_json(run_dir / "summary.json", aggregate)
        write_comparison(run_dir, aggregate)
        return aggregate


def comparison_markdown(result):
    profiles = [e["executor"] for e in result["engines"]]
    cases = list(dict.fromkeys(r["case_id"] for r in result["cases"]))
    lookup = {(r["executor"], r["case_id"]): r["status"] for r in result["cases"]}
    lines = ["# Multi-engine comparison", "", "Frozen suite SHA256: `" + result["suite_sha256"] + "`", "",
             "| Case | " + " | ".join(profiles) + " |", "|---|" + "---|" * len(profiles)]
    for case in cases:
        lines.append("| " + case + " | " + " | ".join(lookup.get((p, case), "NOT_RUN") for p in profiles) + " |")
    lines += ["", "| Executor | Pass | Fail | Error | Seconds | Model |", "|---|---:|---:|---:|---:|---|"]
    for engine in result["engines"]:
        counts = [sum(r["status"] == status for r in engine["cases"]) for status in ("PASS", "FAIL", "ERROR")]
        lines.append(f"| {engine['executor']} | {counts[0]} | {counts[1]} | {counts[2]} | {engine.get('seconds', 'unknown')} | {engine['profile'].get('model', 'local-default')} |")
    lines += ["", "NOT_RUN and ERROR never count as a pass. Missing credentials and unsupported trace formats must be resolved before adoption."]
    return "\n".join(lines) + "\n"


def write_comparison(directory, result):
    (Path(directory) / "comparison.md").write_text(comparison_markdown(result), encoding="utf-8")
