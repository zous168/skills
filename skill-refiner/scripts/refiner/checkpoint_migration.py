"""Offline, explicit migration of an already validated create inputs handoff."""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile

from .artifact_validation import schema_ref, validate_value
from .common import RefinerError, digest, read_json, write_json
from .profiles import load_config
from .stages import StageManifest


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_link(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _authenticated_call_file(path, root, source, stage_files, sealed_files, omissions, *, allow_manifest_raw=False):
    relative = path.relative_to(root).as_posix()
    if root != source:
        # The retained carrier's exact inventory was verified separately.
        return True
    expected, sealed = stage_files.get(relative), sealed_files.get(relative)
    if expected is not None or sealed is not None:
        actual = _sha(path)
        if ((expected is not None and actual != expected) or (sealed is not None and actual != sealed)):
            raise RefinerError(f"Authenticated analysis evidence changed: {relative}")
        return True
    if allow_manifest_raw and path.name == "raw-response.txt":
        manifest_path = path.parent / "request-manifest.json"
        if _is_link(manifest_path) or not manifest_path.is_file() or not manifest_path.resolve().is_relative_to(source):
            raise RefinerError("Invalid-response manifest is missing or unsafe")
        manifest_rel = manifest_path.relative_to(source).as_posix()
        manifest_sha = _sha(manifest_path)
        recorded, sealed = stage_files.get(manifest_rel), sealed_files.get(manifest_rel)
        if ((recorded is None and sealed is None) or (recorded is not None and manifest_sha != recorded)
                or (sealed is not None and manifest_sha != sealed)):
            raise RefinerError("Invalid-response manifest is not authenticated by the source")
        manifest = read_json(manifest_path)
        # Agent.ask hashes the decoded text, while Path.write_text on Windows
        # may store LF as CRLF. Preserve bytes separately in the new inventory.
        raw_text_sha = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if manifest.get("status") != "invalid_response" or raw_text_sha != manifest.get("response_sha256"):
            raise RefinerError("Invalid-response raw evidence does not match its authenticated manifest")
        return True
    pieces = Path(relative).parts
    known_auxiliary = (len(pieces) == 3 and pieces[0] == 'agent-calls'
                       and pieces[2] in {'backend-result.json', 'response.txt'}) or (
                       len(pieces) == 4 and pieces[0] == 'agent-calls'
                       and pieces[2:] == ('cwd', 'response-schema.json'))
    if not known_auxiliary:
        raise RefinerError(f"Unknown unauthenticated analysis evidence: {relative}")
    omissions.add(relative)
    return False


def _usage(value, label, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < 0 or (integer and type(value) is not int)):
        raise RefinerError(f"Invalid {label}: expected a finite nonnegative {'integer' if integer else 'number'}")
    return value


def _controller_hash(root):
    scripts = root / "scripts"
    if not (scripts / "refiner" / "stages.py").is_file():
        raise RefinerError("Archived controller is missing scripts/refiner/stages.py")
    return digest({p.relative_to(scripts).as_posix(): _sha(p) for p in sorted(scripts.rglob("*.py"))
                   if "__pycache__" not in p.parts})


def _inventory(root):
    folder = root / "inputs"
    if not folder.is_dir() or _is_link(folder) or not (root / "inputs.json").is_file():
        raise RefinerError("Missing inputs checkpoint")
    files = [root / "inputs.json"]
    for p in folder.rglob("*"):
        if _is_link(p):
            raise RefinerError("Input checkpoint contains a symlink")
        if p.is_file():
            files.append(p)
    if _is_link(root / "inputs.json"):
        raise RefinerError("Input checkpoint contains a symlink")
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(files)}


def _source_artifact_path(relative, content_sha256):
    """Keep carried analysis artifacts shallow and stable across migrations."""
    prefix = "analysis-previous/source-artifacts/"
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or not isinstance(content_sha256, str) or len(content_sha256) != 64
            or any(char not in "0123456789abcdef" for char in content_sha256)):
        raise RefinerError("Invalid carried analysis artifact identity")
    tail = relative
    while tail.startswith(prefix):
        tail = tail[len(prefix):]
    parts = PurePosixPath(tail).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise RefinerError("Invalid carried analysis artifact path")
    # A prior migration already placed this content under its stable digest
    # directory. Reuse that path instead of wrapping it in another generation.
    if len(parts) == 2 and parts[0] == content_sha256[:24]:
        digest_dir, filename = parts
    else:
        digest_dir, filename = content_sha256[:24], parts[-1]
    return PurePosixPath(prefix + digest_dir + "/" + filename)


def verify_migration(work, contract, state, *, allow_historical_controller=False):
    record = read_json(work / "input-migration.json")
    record_version = record.get("schema_version") if isinstance(record, dict) else None
    schema_id = {"skill-refiner.checkpoint-migration/v1": "skill-refiner.checkpoint-migration",
                 "skill-refiner.checkpoint-migration/v2": "skill-refiner.checkpoint-migration-v2",
                 "skill-refiner.checkpoint-migration/v3": "skill-refiner.checkpoint-migration-v3"}.get(record_version)
    if not schema_id:
        raise RefinerError("Unknown input migration schema version")
    validate_value(record, schema_ref(schema_id), subject="input migration")
    if digest(record) != contract.get("input_migration_sha256"):
        raise RefinerError("Input migration provenance changed")
    prior = record["inherited"]
    prior_seconds = _usage(prior.get("seconds"), "inherited seconds")
    prior_calls = _usage(prior.get("agent_calls"), "inherited agent calls", integer=True)
    prior_evaluations = _usage(prior.get("evaluations"), "inherited evaluations", integer=True)
    state_seconds = _usage(state.get("elapsed_seconds"), "state elapsed seconds")
    state_calls = _usage(state.get("agent_calls"), "state agent calls", integer=True)
    state_creation_calls = _usage(state.get("creation_agent_calls"), "state creation calls", integer=True)
    state_evaluations = _usage(state.get("target_evaluations"), "state target evaluations", integer=True)
    calls_dir = work / "agent-calls"
    existing = max([int(p.name.split('-', 1)[0]) for p in calls_dir.iterdir()
                    if p.is_dir() and p.name.split('-', 1)[0].isdigit()] or [0]) if calls_dir.is_dir() else 0
    if (record["workspace"] != str(work) or record["inputs_sha256"] != contract.get("inputs_sha256")
            or record["execution_sha256"] != contract.get("execution_sha256")
            or record["options"] != contract.get("options") or record["limits"] != contract.get("limits")
            or record["artifacts"] != _inventory(work)
            or state_seconds < prior_seconds or state_calls < prior_calls
            or state_calls < state_creation_calls
            or state_creation_calls > max(existing, prior_calls)
            or state_evaluations < prior_evaluations):
        raise RefinerError("Input migration or inherited budget does not match the frozen contract")
    evidence_hash = record.get("process_reconciliation_sha256")
    if evidence_hash and (not (work / "process-reconciliation.json").is_file()
                          or _sha(work / "process-reconciliation.json") != evidence_hash):
        raise RefinerError("Process reconciliation evidence changed or is missing")
    if record_version == "skill-refiner.checkpoint-migration/v3":
        attestation = record["controller_attestation"]
        current_controller = _controller_hash(Path(__file__).resolve().parents[2])
        mismatches = []
        if record.get("controller_archive") is not None:
            mismatches.append("controller_archive must be null")
        if attestation.get("archive_status") != "unavailable":
            mismatches.append("controller_attestation.archive_status must be unavailable")
        if attestation.get("source_controller_sha256") != record.get("source_controller_sha256"):
            mismatches.append("controller_attestation.source_controller_sha256 must match source_controller_sha256")
        if allow_historical_controller:
            if attestation.get("validation_controller_sha256") != contract.get("controller_sha256"):
                mismatches.append("historical validation hash must match the source resume contract")
        else:
            if attestation.get("validation_controller_sha256") != current_controller:
                mismatches.append("controller_attestation.validation_controller_sha256 must match active controller")
            if contract.get("controller_sha256") != current_controller:
                mismatches.append("creation-resume-contract.controller_sha256 must match active controller")
        if attestation.get("validated_completed_stage_ids") != ["inputs"]:
            mismatches.append("controller_attestation.validated_completed_stage_ids must equal ['inputs']")
        if mismatches:
            raise RefinerError("Revalidated checkpoint provenance failed: " + "; ".join(mismatches))
    if record_version == "skill-refiner.checkpoint-migration/v2":
        source_path = work / "provenance" / "source-inputs.json"
        if (_is_link(source_path) or _is_link(source_path.parent) or not source_path.is_file()
                or not source_path.resolve().is_relative_to(Path(work).resolve())):
            raise RefinerError("Original input snapshot is missing or unsafe")
        source_inputs = read_json(source_path)
        if not isinstance(source_inputs, dict) or not isinstance(source_inputs.get("requirements"), list):
            raise RefinerError("Original input snapshot has an invalid requirements structure")
        original = record["source_artifacts"]
        supplement = record["supplement"]
        index = len(source_inputs["requirements"]) + 1
        relative = f"inputs/requirement-{index}.md"
        supplement_path = work / relative
        if (digest(source_inputs) != record["source_inputs_sha256"]
                or _sha(source_path) != original.get("inputs.json")
                or supplement["requirement_index"] != index or supplement["artifact_path"] != relative
                or not supplement["decision_source"].strip()
                or _is_link(supplement_path) or not supplement_path.is_file()
                or _sha(supplement_path) != supplement["source_sha256"]):
            raise RefinerError("Supplement provenance or original input snapshot changed")
        current = _inventory(work)
        if (relative in original or set(current) != set(original) | {relative}
                or any(current.get(path) != sha for path, sha in original.items() if path != "inputs.json")):
            raise RefinerError("Derived input artifacts do not preserve the original input inventory")
        expected = copy.deepcopy(source_inputs)
        expected["requirements"].append({"source": supplement["source_path"],
                                         "content": supplement_path.read_text(encoding="utf-8")})
        if read_json(work / "inputs.json") != expected:
            raise RefinerError("Derived inputs are not the original requirements plus the declared supplement")
    return prior["agent_calls"]


def import_inputs(args):
    if not args.output or not args.config or args.agent_command or args.engine_config or args.engine:
        raise RefinerError("Migration requires --output and --config, without legacy backend options")
    if args.spec or args.standard:
        raise RefinerError("Migration reuses frozen inputs; do not supply new spec or standards")
    supplement_arg = getattr(args, "supplement", None)
    supplement_source = getattr(args, "supplement_source", None)
    carry_analysis = bool(getattr(args, "carry_analysis_feedback", False))
    carry_technical = bool(getattr(args, "carry_technical_clarification", False))
    if carry_technical and not carry_analysis:
        raise RefinerError("Technical clarification recovery requires carried analysis feedback")
    if carry_analysis and supplement_arg:
        raise RefinerError("Rejected analysis can only be carried with identical business inputs")
    if bool(supplement_arg) != bool(supplement_source and supplement_source.strip()):
        raise RefinerError("An explicit supplement and its user-decision source must be supplied together")
    old = Path(args.source_run).resolve()
    archive = Path(args.controller_archive).resolve() if args.controller_archive else None
    target = Path(args.output).resolve()
    revalidate_source = bool(getattr(args, "revalidate_source_checkpoint", False))
    if not old.is_dir() or (archive is not None and not archive.is_dir()) or target.exists():
        raise RefinerError("Source must exist, any supplied archive must exist, and output must be a new directory")
    isolated_from = (old,) if archive is None else (old, archive)
    if any(target == p or target.is_relative_to(p) or p.is_relative_to(target) for p in isolated_from):
        raise RefinerError("Migration output must be isolated from source and controller archive")
    if revalidate_source and supplement_arg:
        raise RefinerError("Source-controller revalidation cannot be combined with a new business requirement")
    if not archive and not revalidate_source:
        raise RefinerError("An exact source controller archive is required unless --revalidate-source-checkpoint is selected")
    supplement = None
    supplement_bytes = None
    if supplement_arg:
        supplied = Path(supplement_arg).absolute()
        if any(_is_link(p) for p in (supplied, *supplied.parents)) or not supplied.is_file():
            raise RefinerError("Supplement must be a regular file without symlink components")
        supplement = supplied.resolve()
        if (any(supplement == p or supplement.is_relative_to(p) for p in (old, archive, target))
                or target.is_relative_to(supplement)):
            raise RefinerError("Supplement must be separate from source, controller archive, and output")
        supplement_bytes = supplement.read_bytes()
        try:
            supplement_text = supplement_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            raise RefinerError("Supplement must be valid UTF-8") from exc
        if not supplement_text.strip():
            raise RefinerError("Supplement must contain an explicit business decision")
    source_manifest_hash = _sha(old / "stages.json")
    source_contract_hash = _sha(old / "creation-resume-contract.json")
    source_state_hash = _sha(old / "state.json")
    old_contract = read_json(old / "creation-resume-contract.json")
    old_state = read_json(old / "state.json")
    if not isinstance(old_state, dict):
        raise RefinerError("Source state must be an object")
    old_seconds = _usage(old_state.get("elapsed_seconds", 0), "source elapsed seconds")
    old_state_calls = _usage(old_state.get("agent_calls", 0), "source agent calls", integer=True)
    old_creation_calls = _usage(old_state.get("creation_agent_calls", old_state_calls), "source creation calls", integer=True)
    old_evaluations = _usage(old_state.get("target_evaluations", 0), "source target evaluations", integer=True)
    if old_state_calls < old_creation_calls:
        raise RefinerError("Source total agent calls undercount creation calls")
    stages = read_json(old / "stages.json")
    if not isinstance(stages, dict):
        raise RefinerError("Source stage manifest must be an object")
    if old_contract.get("schema_version") != "skill-refiner.creation-resume/v1" or old_state.get("mode") != "create":
        raise RefinerError("Source is not a creation checkpoint")
    if old_contract.get("destination") is not None:
        raise RefinerError("Migration only supports a source with no delivery destination")
    if ((old / "refinement").exists() or (old / "refinement-resume-attempts").exists()
            or any(old.rglob("evaluation-cost.json"))):
        raise RefinerError("Source has refinement or evaluation evidence; inputs-only migration cannot establish zero evaluations")
    source_controller_hash = old_contract.get("controller_sha256")
    archive_hash = _controller_hash(archive) if archive is not None else None
    archive_matches = archive_hash == source_controller_hash
    if not archive_matches and archive is not None:
        raise RefinerError("A supplied controller archive does not match the source; omit it only when explicitly revalidating with the current controller")
    if not archive_matches and not revalidate_source:
        raise RefinerError("An exact source controller archive is required unless --revalidate-source-checkpoint is selected")
    if not archive_matches and not source_controller_hash:
        raise RefinerError("Source controller hash is missing; checkpoint cannot be revalidated")
    validation_controller_hash = _controller_hash(Path(__file__).resolve().parents[2])
    rows = stages.get("stages", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RefinerError("Source stage manifest must contain a stage list")
    completed = [r for r in rows if r.get("status") == "completed"]
    if len(completed) != 1 or completed[0].get("id") != "inputs" or stages.get("active_stage") is not None:
        raise RefinerError("Source must have only inputs completed and no active stage")
    inputs = read_json(old / "inputs.json")
    if digest(inputs) != old_contract.get("inputs_sha256"):
        raise RefinerError("Source inputs semantic hash changed")
    analysis_history = []
    unvalidated_analysis_calls = []
    previous_technical = bool(carry_technical and old_contract.get("technical_recovery_sha256"))
    technical_history, technical_calls = [], []
    latest_pending, latest_source_root, latest_source_files = None, None, {}
    analysis_source_files = {}
    validated_sealed_files = {}
    omitted_unsealed = set()
    if carry_analysis:
        from .analysis_recovery import rejected_history, verify_recovery
        if previous_technical:
            # The preceding technical derivation is not an analyze call. Its
            # hash-pinned triage and source chain seed the subsequent review.
            analysis_history = verify_recovery(old, old_contract, inputs,
                                               unvalidated_calls=unvalidated_analysis_calls)
            validated_sealed_files = read_json(old / "analysis-recovery.json")["files"]
        else:
            analysis_history = rejected_history(old, inputs, source_manifest=stages,
                                                allow_pending_clarification=carry_technical,
                                                unvalidated_calls=unvalidated_analysis_calls)
            if old_contract.get("analysis_recovery_sha256"):
                prior = read_json(old / "analysis-recovery.json")
                validate_value(prior, schema_ref("skill-refiner.analysis-recovery"), subject="prior analysis recovery")
                if digest(prior) != old_contract["analysis_recovery_sha256"] or prior["inputs_sha256"] != digest(inputs):
                    raise RefinerError("Prior analysis recovery provenance or inputs changed")
                validated_sealed_files = prior["files"]
                for relative, sha in validated_sealed_files.items():
                    path = old / relative
                    if (Path(relative).is_absolute() or _is_link(path) or not path.is_file()
                            or not path.resolve().is_relative_to(old) or _sha(path) != sha):
                        raise RefinerError(f"Prior analysis recovery evidence changed: {relative}")
        if not analysis_history:
            raise RefinerError("No verified independently rejected analysis is available to carry")
        stage_row = next(row for row in stages["stages"] if row["id"] == "analysis")
        stage_files = {row["path"]: row["sha256"] for row in stage_row.get("artifacts", [])}

        if carry_technical:
            from .technical_recovery import clarification_history, verify_technical_recovery
            if previous_technical:
                prior_pair = verify_technical_recovery(old, old_contract, inputs)
                technical_history, technical_calls = clarification_history(
                    old / "analysis-previous" / "technical-history", inputs, after_call=0)
                if prior_pair != technical_history[-1]:
                    raise RefinerError("Prior technical chain differs from its verified contract")
                from .clarification_recovery import latest_clarification, verify_clarification_recovery
                retained_latest = verify_clarification_recovery(old, old_contract, inputs, analysis_history)
                # verify_recovery has checked the inherited root-call bytes and
                # verify_clarification_recovery has checked the sealed carrier.
                # These calls are not outputs of a new, still-pending stage.
                sealed_boundary = max((int(name.split("-", 1)[0])
                                       for name in retained_latest["calls"]), default=0) if retained_latest else 0
                active_latest = latest_clarification(
                    old, inputs, analysis_history, source_manifest=stages,
                    validated_clarification_boundary=sealed_boundary)
                latest_pending = active_latest or retained_latest
                if latest_pending:
                    latest_source_root = old if active_latest else old / "analysis-previous" / "latest-clarification"
                    for name in latest_pending["calls"]:
                        for path in (latest_source_root / "agent-calls" / name).rglob("*"):
                            if _is_link(path):
                                raise RefinerError("Unsafe link in latest clarification call evidence")
                            if path.is_file():
                                relative = path.relative_to(latest_source_root).as_posix()
                                if _authenticated_call_file(path, latest_source_root, old, stage_files,
                                                            validated_sealed_files, omitted_unsealed, allow_manifest_raw=True):
                                    latest_source_files[relative] = _sha(path)
            else:
                after_call = int(analysis_history[-1]["review_call"].split("-", 1)[0])
                technical_history, technical_calls = clarification_history(old, inputs, after_call=after_call,
                                                                             source_manifest=stages)
            if not previous_technical and read_json(old / "analysis.json") != technical_history[-1]["analysis"]:
                raise RefinerError("Stored pending analysis does not match the latest clarification chain")
        names = {row[key] for row in analysis_history for key in ("analysis_call", "review_call")}
        if previous_technical:
            names.difference_update(technical_calls)
            # Preserve every subsequent call, including failed and triage calls,
            # even when it is not itself a completed rejected pair.
            calls = old / "agent-calls"
            names.update(path.name for path in calls.iterdir() if path.is_dir()
                         and path.name.endswith(("-analyze", "-analysis-review")))
        else:
            names.update(technical_calls)
        names.update(unvalidated_analysis_calls)
        for relative, sha in stage_files.items():
            if any(relative.startswith(f"agent-calls/{name}/") for name in names):
                path = old / relative
                if _is_link(path) or not path.is_file() or not path.resolve().is_relative_to(old) or _sha(path) != sha:
                    raise RefinerError(f"Recorded analysis evidence is missing or changed: {relative}")
        for name in names:
            directory = old / "agent-calls" / name
            for path in directory.rglob("*"):
                if _is_link(path):
                    raise RefinerError("Analysis recovery call evidence contains a symlink")
                if path.is_file():
                    relative = path.relative_to(old).as_posix()
                    if not _authenticated_call_file(
                            path, old, old, stage_files, validated_sealed_files, omitted_unsealed,
                            allow_manifest_raw=(name in technical_calls or bool(latest_pending and name in latest_pending['calls']))):
                        continue
                    analysis_source_files[relative] = _sha(path)
                    if (relative in validated_sealed_files
                            and validated_sealed_files[relative] != analysis_source_files[relative]):
                        raise RefinerError(f"Sealed analysis recovery evidence changed: {relative}")
                    if relative in stage_files and stage_files[relative] != analysis_source_files[relative]:
                        raise RefinerError(f"Analysis recovery evidence changed: {relative}")
        source_artifacts = ([path for pattern in ("analysis-attempt-*.json", "analysis-review-attempt-*.json",
                                                  "analysis-revision-delta-*.json", "clarification-review-attempt-*.json",
                                                  "analysis-derived-technical-*.json", "technical-triage-*.json")
                             for path in sorted(old.glob(pattern))] + [old / "analysis-review.json", old / "analysis.json"])
        if previous_technical:
            source_artifacts.append(old / "technical-coverage.json")
        for path in source_artifacts:
            if path.is_file():
                relative = path.relative_to(old).as_posix()
                sha = _sha(path)
                if (_is_link(path)
                        or (relative in validated_sealed_files and validated_sealed_files[relative] != sha)
                        or (relative in stage_files and stage_files[relative] != sha)
                        or (relative not in validated_sealed_files and relative not in stage_files)):
                    raise RefinerError(f"Original analysis artifact changed: {relative}")
                analysis_source_files[relative] = _sha(path)
        if old_contract.get("analysis_recovery_sha256"):
            recovery_path = old / "analysis-recovery.json"
            if _is_link(recovery_path) or not recovery_path.is_file():
                raise RefinerError("Prior analysis recovery provenance is missing or unsafe")
            # The prior manifests are validated above and again against their
            # contracts. The new migration writes replacement manifests over
            # the same authenticated call/history evidence, so carrying these
            # derived manifests as artifacts would grow the recovery chain on
            # every migration.
            if previous_technical:
                tech_record = old / "technical-recovery.json"
                if _is_link(tech_record) or not tech_record.is_file():
                    raise RefinerError("Prior technical provenance is missing or unsafe")
            previous_dir = old / "analysis-previous"
            if _is_link(previous_dir):
                raise RefinerError("Prior analysis recovery provenance directory is unsafe")
            if previous_dir.is_dir():
                for path in previous_dir.rglob("*"):
                    if _is_link(path):
                        raise RefinerError("Prior analysis provenance contains a symlink")
                    if path.is_file():
                        relative = path.relative_to(old).as_posix()
                        sha = _sha(path)
                        if old_contract.get("analysis_recovery_sha256") and validated_sealed_files.get(relative) != sha:
                            raise RefinerError(f"Prior analysis provenance is not sealed or changed: {relative}")
                        if path.name not in {"analysis-recovery.json", "technical-recovery.json",
                                             "clarification-recovery.json"}:
                            analysis_source_files[relative] = sha
    if supplement and any(row.get("source") == str(supplement) or row.get("content") == supplement_text
                          for row in inputs.get("requirements", [])):
        raise RefinerError("Supplement duplicates an existing requirement source or content")
    inventory = _inventory(old)
    artifacts = completed[0].get("artifacts", [])
    recorded = {r.get("path"): r.get("sha256") for r in artifacts}
    if (len(recorded) != len(artifacts) or inventory != recorded or completed[0].get("missing_outputs")
            or completed[0].get("input_artifacts") != [] or completed[0].get("missing_inputs")):
        raise RefinerError("Source inputs inventory differs from the completed checkpoint")
    # Validate the checkpoint in a disposable copy. For an explicit current-
    # controller revalidation, do not ask the current StageManifest to resume
    # the historical registry: that would reject an otherwise reusable inputs
    # checkpoint solely because schema hashes changed. Instead, authenticate
    # the old inputs-stage declaration and source inventory above, construct a
    # fresh current manifest, and validate only the completed inputs handoff.
    with tempfile.TemporaryDirectory(prefix="refiner-input-check-") as temp:
        check = Path(temp) / old.name
        check.mkdir()
        for rel in inventory:
            dst = check / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old / rel, dst)
        if revalidate_source:
            source_ids = [row.get("id") for row in rows]
            if (len(source_ids) != len(rows) or len(set(source_ids)) != len(source_ids)
                    or "inputs" not in source_ids or stages.get("mode") != "create"
                    or stages.get("run_id") != old.name or stages.get("active_stage") is not None):
                raise RefinerError("Historical stage manifest cannot identify a safe inputs-only checkpoint")
            source_input = next(row for row in rows if row.get("id") == "inputs")
            current_input = StageManifest._bound_contract("create")[0]
            for field in ("inputs", "outputs"):
                stored = source_input.get(field)
                current = current_input.get(field)
                if (not isinstance(stored, list) or not isinstance(current, list)
                        or [{k: v for k, v in item.items() if k != "schema_ref"}
                            for item in stored if isinstance(item, dict)]
                        != [{k: v for k, v in item.items() if k != "schema_ref"}
                            for item in current if isinstance(item, dict)]
                        or any(not isinstance(item, dict) for item in stored)):
                    raise RefinerError(f"Historical inputs-stage {field} contract differs from the current contract")
            for field in ("validation_gate", "failure_states"):
                if source_input.get(field) != current_input.get(field):
                    raise RefinerError(f"Historical inputs-stage {field} contract differs from the current contract")
            current_manifest = StageManifest(check, "create")
            current_manifest.complete("inputs")
            revalidated = {item["path"]: item["sha256"]
                           for item in current_manifest.data["stages"][0]["artifacts"]}
            if revalidated != recorded:
                raise RefinerError("Current controller revalidated a different inputs artifact inventory")
        else:
            shutil.copy2(old / "stages.json", check / "stages.json")
        prior = Path(temp) / "prior-recovery"
        if carry_technical and old_contract.get("analysis_recovery_sha256") and not previous_technical:
            prior.mkdir()
            old_recovery = read_json(old / "analysis-recovery.json")
            for rel, sha in old_recovery.get("files", {}).items():
                source = old / rel
                if Path(rel).is_absolute() or not source.resolve().is_relative_to(old) or _is_link(source) or not source.is_file() or _sha(source) != sha:
                    raise RefinerError(f"Prior analysis recovery evidence changed: {rel}")
                dst = prior / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dst)
            shutil.copy2(old / "analysis-recovery.json", prior / "analysis-recovery.json")
            shutil.copy2(old / "inputs.json", prior / "inputs.json")
        script = ("import sys;sys.path.insert(0,sys.argv[1]);from refiner.stages import StageManifest;"
                  "StageManifest(sys.argv[2], 'create', resume=True);"
                  "from refiner.common import read_json;from pathlib import Path;"
                  "p=Path(sys.argv[3]);c=read_json(p/'creation-resume-contract.json');"
                  "exec(\"if c.get('input_migration_sha256'):\\n from refiner.checkpoint_migration import verify_migration\\n verify_migration(p,c,read_json(p/'state.json'),allow_historical_controller=(sys.argv[5]=='1'))\\n"
                  "if c.get('analysis_recovery_sha256'):\\n from refiner.analysis_recovery import verify_recovery\\n q=Path(sys.argv[4]) if sys.argv[4] else p\\n verify_recovery(q,c,read_json(q/'inputs.json'))\")")
        current_scripts = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-B", "-c", script, str(current_scripts), str(check), str(old), str(prior) if prior.exists() else "",
                                 "1" if revalidate_source else "0"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RefinerError("Archived checkpoint/schema validation failed: " + result.stderr[-2500:])
        if (_inventory(check) != inventory or _inventory(old) != inventory
                or _sha(old / "stages.json") != source_manifest_hash
                or _sha(old / "creation-resume-contract.json") != source_contract_hash
                or _sha(old / "state.json") != source_state_hash
                or (archive is not None and _controller_hash(archive) != archive_hash)):
            raise RefinerError("Inputs changed during checkpoint verification")
        if supplement and supplement.read_bytes() != supplement_bytes:
            raise RefinerError("Supplement changed during checkpoint verification")
        calls_dir = old / "agent-calls"
        old_calls = max([int(p.name.split('-', 1)[0]) for p in calls_dir.iterdir()
                         if p.is_dir() and p.name.split('-', 1)[0].isdigit()] or [0]) if calls_dir.is_dir() else 0
        evidence = None
        evidence_hash = None
        if max(old_calls, old_state_calls):
            if not args.process_reconciliation:
                raise RefinerError("Prior calls require explicit external process reconciliation evidence")
            evidence = Path(args.process_reconciliation).resolve()
            evidence_hash = _sha(evidence)
            reconciliation = read_json(evidence)
            if (not isinstance(reconciliation, dict) or reconciliation.get("exit_code") != 0
                    or reconciliation.get("processes") != [] or not isinstance(reconciliation.get("query"), str)
                    or old.name not in reconciliation["query"]
                    or any(not isinstance(reconciliation.get(k), str) or not reconciliation[k].strip()
                           for k in ("checked_at", "historical_evidence", "limitation"))):
                raise RefinerError("Process reconciliation must identify the source run, document historical termination and limitations, and find no matching process")
            if _sha(evidence) != evidence_hash:
                raise RefinerError("Process reconciliation evidence changed during validation")
        prior_seconds = _usage(args.prior_seconds, "inherited seconds")
        prior_calls = _usage(args.prior_agent_calls, "inherited agent calls", integer=True)
        prior_evaluations = _usage(args.prior_evaluations, "inherited evaluations", integer=True)
        if (prior_seconds < old_seconds or prior_calls < max(old_calls, old_state_calls)
                or prior_evaluations != 0 or old_evaluations != 0
                or args.prior_seconds >= args.max_seconds or args.prior_agent_calls >= args.max_agent_calls):
            raise RefinerError("Inherited consumption must cover source evidence and leave remaining budget; nonzero prior evaluations are unsupported")
        config = load_config(args.config)
        execution = {"backend_config": config, "agent_command": None, "engine": None, "engine_config": None,
                     "skill_up": args.skill_up, "timeout": args.timeout, "parallelism": args.parallelism,
                     "agent_parallelism": args.agent_parallelism}
        options = {k: getattr(args, k) for k in ("max_rounds", "planning_attempts", "max_cases", "acceptance_cases")}
        limits = {k: getattr(args, k) for k in ("max_seconds", "max_agent_calls", "max_evaluations", "max_rounds", "planning_attempts", "max_cases", "acceptance_cases")}
        revalidated_without_archive = archive is None
        record = {"schema_version": "skill-refiner.checkpoint-migration/v3" if revalidated_without_archive
                  else "skill-refiner.checkpoint-migration/v1", "workspace": str(target),
                  "source_run": str(old), "controller_archive": str(archive) if archive else None,
                  "source_controller_sha256": source_controller_hash,
                  "source_manifest_sha256": source_manifest_hash, "source_contract_sha256": source_contract_hash,
                  "source_state_sha256": source_state_hash,
                  "process_reconciliation_sha256": evidence_hash,
                  "inputs_sha256": digest(inputs), "artifacts": inventory,
                  "inherited": {"seconds": args.prior_seconds, "agent_calls": args.prior_agent_calls, "evaluations": args.prior_evaluations},
                  "execution_sha256": digest(execution), "options": options, "limits": limits}
        if revalidated_without_archive:
            record["controller_attestation"] = {
                "archive_status": "unavailable",
                "validation_method": "current-controller-stage-schema-and-content-hash-revalidation",
                "source_controller_sha256": source_controller_hash,
                "validation_controller_sha256": validation_controller_hash,
                "validated_completed_stage_ids": ["inputs"],
                "untrusted_stage_ids": [row["id"] for row in rows if row.get("id") != "inputs"],
            }
        target.mkdir(parents=True)
        try:
            for rel in inventory:
                dst = target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(check / rel, dst)
            if _inventory(target) != inventory:
                raise RefinerError("Copied inputs failed hash verification")
            derived_inputs = inputs
            if supplement:
                index = len(inputs["requirements"]) + 1
                relative = f"inputs/requirement-{index}.md"
                if relative in inventory:
                    raise RefinerError("Supplement would overwrite an existing input artifact")
                provenance = target / "provenance"
                provenance.mkdir()
                shutil.copy2(check / "inputs.json", provenance / "source-inputs.json")
                (target / relative).write_bytes(supplement_bytes)
                derived_inputs = copy.deepcopy(inputs)
                derived_inputs["requirements"].append({"source": str(supplement), "content": supplement_text})
                write_json(target / "inputs.json", derived_inputs)
                record.update(schema_version="skill-refiner.checkpoint-migration/v2", mode="append-user-requirement",
                              source_inputs_sha256=digest(inputs), source_artifacts=inventory,
                              supplement={"source_path": str(supplement), "source_sha256": hashlib.sha256(supplement_bytes).hexdigest(),
                                          "decision_source": supplement_source.strip(), "artifact_path": relative,
                                          "requirement_index": index},
                              inputs_sha256=digest(derived_inputs), artifacts=_inventory(target))
            recovery = None
            clarification = None
            if carry_analysis:
                copied = {}
                for relative, sha in analysis_source_files.items():
                    source = old / relative
                    if _sha(source) != sha:
                        raise RefinerError(f"Analysis recovery evidence changed during migration: {relative}")
                    technical_call = (not previous_technical and relative.startswith("agent-calls/")
                                      and relative.split("/")[1] in technical_calls)
                    unvalidated_analysis_call = (
                        relative.startswith("agent-calls/")
                        and relative.split("/")[1] in unvalidated_analysis_calls)
                    retained_technical = previous_technical and relative.startswith("analysis-previous/technical-history/")
                    active_technical = previous_technical and (relative == "technical-coverage.json"
                                       or relative.startswith("technical-triage-"))
                    if unvalidated_analysis_call:
                        # Keep the failed response sealed as historical evidence,
                        # outside Agent's live response cache, so resume makes a
                        # fresh call instead of replaying the same invalid JSON.
                        destination = target.joinpath(*_source_artifact_path(relative, sha).parts)
                    elif retained_technical or active_technical:
                        destination = target / relative
                    elif technical_call:
                        destination = target / "analysis-previous" / "technical-history" / relative
                    elif relative.startswith("agent-calls/"):
                        destination = target / relative
                    else:
                        destination = target.joinpath(*_source_artifact_path(relative, sha).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        if _sha(destination) != sha:
                            raise RefinerError("Carried analysis artifact path collision")
                    else:
                        shutil.copy2(source, destination)
                    copied[destination.relative_to(target).as_posix()] = sha
                if latest_pending:
                    latest_copied = {}
                    for relative, sha in latest_source_files.items():
                        source = latest_source_root / relative
                        if _sha(source) != sha:
                            raise RefinerError("Latest clarification changed during migration")
                        destination = target / "analysis-previous" / "latest-clarification" / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                        key = destination.relative_to(target).as_posix()
                        latest_copied[key] = _sha(destination)
                        copied[key] = latest_copied[key]
                    clarification = {
                        "schema_version": "skill-refiner.clarification-recovery/v1",
                        "inputs_sha256": digest(inputs),
                        "after_review_call": latest_pending["after_review_call"],
                        "after_review_sha256": latest_pending["after_review_sha256"],
                        "analysis_sha256": digest(latest_pending["pair"]["analysis"]),
                        "clarification_sha256": digest(latest_pending["pair"]["review"]),
                        "calls": latest_pending["calls"], "files": latest_copied}
                    validate_value(clarification, schema_ref("skill-refiner.clarification-recovery"),
                                   subject="latest clarification recovery")
                if omitted_unsealed:
                    omission_path = target / "analysis-previous" / "unsealed-omissions.json"
                    write_json(omission_path, {"source_run": str(old),
                        "reason": "Historical source did not authenticate these auxiliary files; excluded from recovery.",
                        "provenance": "This omission list was generated at migration and is not an original stage attestation.",
                        "paths": sorted(omitted_unsealed)})
                    copied[omission_path.relative_to(target).as_posix()] = _sha(omission_path)
                recovery = {"schema_version": "skill-refiner.analysis-recovery/v1", "mode": "rejected-analysis-evidence",
                            "source_run": str(old), "source_manifest_sha256": source_manifest_hash,
                            "source_controller_sha256": source_controller_hash, "inputs_sha256": digest(inputs),
                            "pairs": [{"analysis_call": row["analysis_call"], "review_call": row["review_call"],
                                       "analysis_sha256": digest(row["analysis"]), "review_sha256": digest(row["review"]),
                                        "approved": False} for row in analysis_history
                                       if (old / "agent-calls" / row["analysis_call"]).is_dir()], "files": copied}
                validate_value(recovery, schema_ref("skill-refiner.analysis-recovery"), subject="analysis recovery")
            technical = None
            if carry_technical:
                technical_files = {key: value for key, value in copied.items()
                                   if key.startswith("analysis-previous/technical-history/")}
                technical = {"schema_version": "skill-refiner.technical-recovery/v1", "inputs_sha256": digest(inputs),
                             "analysis_sha256": digest(technical_history[-1]["analysis"]),
                             "clarification_sha256": digest(technical_history[-1]["review"]),
                             "calls": technical_calls, "files": technical_files}
                validate_value(technical, schema_ref("skill-refiner.technical-recovery"), subject="technical recovery")
            schema_id = ("skill-refiner.checkpoint-migration-v3" if revalidated_without_archive else
                         "skill-refiner.checkpoint-migration-v2" if supplement else
                         "skill-refiner.checkpoint-migration")
            validate_value(record, schema_ref(schema_id), subject="input migration")
            stage = StageManifest(target, "create")
            stage.complete("inputs")
            controller = _controller_hash(Path(__file__).resolve().parents[2])
            contract = {"schema_version": "skill-refiner.creation-resume/v1", "inputs_sha256": digest(derived_inputs),
                        "execution_sha256": digest(execution), "controller_sha256": controller, "destination": None,
                        "options": options, "limits": limits, "input_migration_sha256": digest(record)}
            if recovery:
                write_json(target / "analysis-recovery.json", recovery)
                contract["analysis_recovery_sha256"] = digest(recovery)
            if technical:
                write_json(target / "technical-recovery.json", technical)
                contract["technical_recovery_sha256"] = digest(technical)
            if clarification:
                write_json(target / "clarification-recovery.json", clarification)
                contract["clarification_recovery_sha256"] = digest(clarification)
            state = {"status": "interrupted", "mode": "create", "workspace": str(target), "questions": [],
                     "elapsed_seconds": args.prior_seconds, "agent_calls": args.prior_agent_calls,
                     "creation_agent_calls": args.prior_agent_calls, "target_evaluations": 0, "evaluation_history": [], "limits": limits}
            if previous_technical:
                state["technical_coverage_sha256"] = old_state.get("technical_coverage_sha256")
            write_json(target / "input-migration.json", record)
            if evidence:
                shutil.copy2(evidence, target / "process-reconciliation.json")
            write_json(target / "creation-resume-contract.json", contract)
            write_json(target / "state.json", state)
            verify_migration(target, contract, state)
            if recovery:
                from .analysis_recovery import verify_recovery
                verify_recovery(target, contract, derived_inputs)
            if technical:
                from .technical_recovery import verify_technical_recovery
                verify_technical_recovery(target, contract, derived_inputs)
            if clarification:
                from .clarification_recovery import verify_clarification_recovery
                verify_clarification_recovery(target, contract, derived_inputs,
                                             verify_recovery(target, contract, derived_inputs))
            if (_inventory(old) != inventory or _sha(old / "stages.json") != source_manifest_hash
                    or _sha(old / "creation-resume-contract.json") != source_contract_hash
                    or _sha(old / "state.json") != source_state_hash
                    or (supplement and supplement.read_bytes() != supplement_bytes)):
                raise RefinerError("Source or supplement changed during migration")
        except Exception:
            shutil.rmtree(target)
            raise
    if supplement:
        return {"status": "inputs_derived", "workspace": str(target), "source_inputs_validated": True,
                "added_requirement": record["supplement"], "inherited": record["inherited"]}
    return {"status": "inputs_imported", "workspace": str(target), "reused_stages": ["inputs"],
            "rejected_analysis_pairs": len(analysis_history), "inherited": record["inherited"]}
