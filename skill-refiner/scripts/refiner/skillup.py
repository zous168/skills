from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import shutil
import sys
import time
import os
import errno
import stat
import shlex
import tempfile
from contextlib import contextmanager, nullcontext

from .bootstrap import PROJECT_ROOT
from .common import RefinerError, read_json, run_process, write_json


@contextmanager
def local_judge_lock(deadline):
    """v0.13.0 names local judge dirs with a coarse clock; avoid collisions."""
    if os.name == "nt":
        import msvcrt
        lock = Path(tempfile.gettempdir()) / "skill-refiner-judge.lock"
    else:
        import fcntl
        lock = Path(tempfile.gettempdir()) / f"skill-refiner-judge-{os.getuid()}.lock"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        try:
            descriptor = os.open(lock, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = os.open(lock, flags)
    except OSError as exc:
        raise RefinerError(f"Cannot safely open the local skill-up judge lock: {exc}") from exc
    acquired = False

    def check_identity():
        opened = os.fstat(descriptor)
        current = os.lstat(lock)
        reparse = getattr(current, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode) or reparse
                or opened.st_nlink != 1 or current.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise RefinerError("Local skill-up judge lock path is unsafe or was replaced")
        if os.name != "nt" and (opened.st_uid != os.getuid() or opened.st_mode & 0o077):
            raise RefinerError("Local skill-up judge lock has unsafe ownership or permissions")

    try:
        check_identity()
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise RefinerError("Wall-clock budget exhausted waiting for the local skill-up judge lock")
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                check_identity()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) and getattr(exc, "winerror", None) not in (33, 36):
                    raise RefinerError(f"Cannot acquire the local skill-up judge lock: {exc}") from exc
                if deadline is not None and time.monotonic() >= deadline:
                    raise RefinerError("Wall-clock budget exhausted waiting for the local skill-up judge lock")
                time.sleep(0.05)
        yield
    finally:
        if acquired and os.name == "nt":
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)
        else:
            os.close(descriptor)


def locate(explicit=None):
    executable = explicit or shutil.which("skill-up") or str(PROJECT_ROOT / ".tools" / "skill-up")
    if not Path(executable).is_file():
        raise RefinerError("skill-up missing. Run: skill-refiner bootstrap")
    return str(Path(executable).resolve())


class _CaseProgress:
    """Translate untrusted skill-up status lines into bounded provisional events."""
    _ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    _line = re.compile(r"^\s*(⏳|✅|❌|⚠️?|�+|\?+)\s+\[(\d+)/(\d+)\]\s*([^:\s]+):\s*(.*?)\s*$")
    _result = re.compile(r"^(PASS|FAIL|ERROR)(?:\s*\([0-9.]+%\))?\s*$")

    def __init__(self, case_ids, engine, context=None):
        self.case_ids = tuple(case_ids)
        context = context if isinstance(context, dict) else {}
        value = context.get("suite")
        self.suite = value if isinstance(value, str) and value in {"baseline", "regression", "acceptance"} else None
        value = context.get("round")
        self.round = value if type(value) is int and 0 <= value <= 10000 else None
        value = context.get("executor")
        self.executor = value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) else None
        self.engine = engine if isinstance(engine, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", engine) else "configured"
        self.pending = ""
        self.discarding = False
        self.started = set()
        self.provisional = {}
        self.finalized = set()
        self.next_index = 0
        self.active = None

    def _emit(self, case_id, status, evidence_ref=None):
        if case_id not in self.case_ids or status not in {
            "started", "provisional_pass", "provisional_fail", "provisional_error",
            "final_pass", "final_fail", "final_error"}:
            return
        event = {"stage": "case_progress", "case_id": case_id, "status": status, "engine": self.engine,
                 "suite": self.suite, "round": self.round, "executor": self.executor}
        if evidence_ref is not None:
            event["evidence_ref"] = evidence_ref
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def feed(self, stream, chunk):
        if stream != "stdout":
            return
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        if not isinstance(chunk, str):
            return
        self.pending += chunk
        while True:
            match = re.search(r"[\r\n]", self.pending)
            if match is None:
                if len(self.pending) > 65536:
                    self.pending = ""
                    self.discarding = True
                return
            line, self.pending = self.pending[:match.start()], self.pending[match.end():]
            if self.discarding:
                self.discarding = False
                continue
            self._accept(line)

    def finish(self):
        if self.pending and not self.discarding:
            self._accept(self.pending)
        self.pending = ""

    def _accept(self, text):
        match = self._line.fullmatch(self._ansi.sub("", text))
        if not match:
            return
        icon, number, total, case_id, tail = match.groups()
        index, total = int(number), int(total)
        if total != len(self.case_ids) or index != self.next_index + 1 or index > total or self.case_ids[index - 1] != case_id:
            return
        fallback_icon = icon.startswith(("�", "?"))
        if tail == case_id + "...":
            if self.active is None and (icon == "⏳" or fallback_icon):
                self.active = case_id
                self.started.add(case_id)
                self._emit(case_id, "started")
            return
        result = self._result.fullmatch(tail)
        if not result or self.active != case_id:
            return
        value = result.group(1)
        valid_icons = {"PASS": {"✅"}, "FAIL": {"❌"}, "ERROR": {"❌", "⚠", "⚠️"}}
        if not fallback_icon and icon not in valid_icons[value]:
            return
        status = "provisional_" + value.lower()
        self.provisional[case_id] = status
        self._emit(case_id, status)
        self.active = None
        self.next_index += 1

    def finalize(self, results, evidence_ref):
        for row in results:
            case_id, status = row["id"], row["status"]
            if case_id in self.case_ids and case_id not in self.finalized:
                self.finalized.add(case_id)
                self._emit(case_id, "final_" + status.lower(), evidence_ref=evidence_ref)

    def abort(self):
        for case_id in self.case_ids:
            if (case_id in self.started or case_id in self.provisional) and case_id not in self.finalized:
                self.finalized.add(case_id)
                self._emit(case_id, "final_error")


_OBS_SCHEMA = Path(__file__).resolve().parents[2] / "schemas/v1/case-observation.schema.json"
_OBS_LIMIT = 2_097_152


def _sensitive_observation(value):
    """Reject known credentials and common direct identifiers before persistence."""
    from .native_tools import sanitize_tool_value
    if isinstance(value, dict):
        for key, child in value.items():
            if sanitize_tool_value({str(key): "sentinel"}) != {str(key): "sentinel"}:
                return True
            if _sensitive_observation(str(key)) or _sensitive_observation(child):
                return True
        return False
    if isinstance(value, list):
        return any(_sensitive_observation(child) for child in value)
    if not isinstance(value, str):
        return False
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return _sensitive_observation(parsed)
    if sanitize_tool_value(value) != value:
        return True
    # RFC-reserved domains and NANP fictional numbers are bounded fixture exceptions.
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@(?:example\.(?:com|org|net)|[A-Z0-9.-]+\.(?:example|invalid))\b", "[fixture-email]", value)
    text = re.sub(r"(?<!\d)(?:\+1[ -]?)?(?:202[ -]?)?555[ -]?01\d{2}(?!\d)", "[fixture-phone]", text)
    return bool(re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[0-9Xx](?!\d)", text))


def _safe_evidence_path(root, value):
    from .common import relative_path
    root = Path(root).resolve()
    path = relative_path(root, value)
    for parent in [root, *(root / Path(*Path(value).parts[:i]) for i in range(1, len(Path(value).parts) + 1))]:
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RefinerError("Case evidence contains a linked path")
    return path


def _minimal_transcript(value, checks, root):
    """Keep only call identity, commands and results needed by these checks."""
    from .checks import command_forms
    entries = []
    def visit(item):
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            role = item.get("role")
            if role == "tool_call" and isinstance(item.get("tool_call"), dict):
                call = item["tool_call"]
                args = call.get("arguments", {})
                if isinstance(args, str):
                    try: args = json.loads(args)
                    except ValueError: args = {}
                if isinstance(args, dict) and isinstance(args.get("arguments"), str):
                    try: args = json.loads(args["arguments"])
                    except ValueError: args = {}
                command = args.get("command") if isinstance(args, dict) else None
                if command is None and str(call.get("name", "")).split(".")[-1] == "exec_command" and isinstance(args, dict):
                    command = args.get("cmd")
                selected = {"id": call.get("id"), "name": call.get("name", ""), "arguments": {}}
                if isinstance(command, str):
                    selected["arguments"]["command"] = command
                entries.append({"role": "tool_call", "tool_call": selected})
            elif role == "tool_result" and isinstance(item.get("tool_result"), dict):
                source = item["tool_result"]
                selected = {key: source[key] for key in ("call_id", "content", "status", "exit_code") if key in source}
                entries.append({"role": "tool_result", "tool_result": selected})
            elif role == "system" and isinstance(item.get("content"), str) and any(
                    marker in item["content"] for marker in ("skill_refiner_native_observation", "skill_refiner_command_exit_evidence")):
                try:
                    system = json.loads(item["content"])
                except (ValueError, TypeError):
                    system = {key: {"status": "incomplete"} for key in
                              ("skill_refiner_native_observation", "skill_refiner_command_exit_evidence")
                              if key in item["content"]}
                entries.append({"role": "system", "system": system})
            for key, child in item.items():
                if key not in {"content", "arguments"}:
                    visit(child)
    visit(value)
    matched = set()
    for row in entries:
        if row["role"] != "tool_call":
            continue
        call = row["tool_call"]
        command = call["arguments"].get("command")
        if not isinstance(command, str):
            continue
        for check in checks:
            if check.get("type") == "tool_output_matches" and any(
                    re.search(str(check.get("value")), form) for form in
                    command_forms(command, root, check.get("literal_command", False))):
                matched.add(call.get("id"))
    result = []
    for row in entries:
        if row["role"] == "tool_result":
            if row["tool_result"].get("call_id") in matched:
                result.append(row)
        elif row["role"] == "system":
            source = row["system"] if isinstance(row["system"], dict) else {}
            minimal = {}
            observation = source.get("skill_refiner_native_observation")
            if observation is not None:
                minimal["skill_refiner_native_observation"] = ({key: observation[key] for key in
                    ("status", "unclassified_tools", "absence_of_external_calls_proven", "absence_of_side_effects_proven")
                    if key in observation} if isinstance(observation, dict) else {"status": "incomplete"})
            command = source.get("skill_refiner_command_exit_evidence")
            if command is not None:
                if isinstance(command, dict):
                    reduced = {key: command[key] for key in ("producer", "schema_version", "status") if key in command}
                    values = command.get("results")
                    reduced["results"] = ({key: value for key, value in values.items() if key in matched}
                                          if isinstance(values, dict) else values)
                    minimal["skill_refiner_command_exit_evidence"] = reduced
                else:
                    minimal["skill_refiner_command_exit_evidence"] = {"status": "incomplete"}
            result.append({"role": "system", "content": json.dumps(minimal, ensure_ascii=False)})
        else:
            result.append(row)
    return result


def _observation_source(case, root, response):
    """Capture only inputs needed by frozen checks, without executing target code."""
    root = Path(root).resolve()
    files_needed = {}
    tools = {"tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}
    needs_trace = any(check.get("type") in tools for check in case["checks"])
    for check in case["checks"]:
        kind = check.get("type", "")
        if kind.startswith("file_") or kind == "json_path_matches_file":
            name = check.get("path")
            if not isinstance(name, str):
                raise RefinerError("File assertion lacks a safe path")
            if _sensitive_observation(name):
                raise RefinerError("Case evidence path contains sensitive content")
            _safe_evidence_path(root, name)
            files_needed[name] = files_needed.get(name, False) or kind != "file_exists"
    if len(files_needed) > 100:
        raise RefinerError("Case evidence contains too many file paths")
    file_data, metadata, total = {}, [], 0
    for name, needs_content in sorted(files_needed.items()):
        path = _safe_evidence_path(root, name)
        present = path.is_file()
        content = bool(present and needs_content)
        if content and path.stat().st_size > _OBS_LIMIT:
            raise RefinerError("Case evidence file exceeds its size limit")
        data = path.read_bytes() if content else b""
        if len(data) > _OBS_LIMIT:
            raise RefinerError("Case evidence file exceeds its size limit")
        if content:
            try: text = data.decode("utf-8")
            except UnicodeError as exc: raise RefinerError("Case evidence is not UTF-8") from exc
            if _sensitive_observation(text):
                raise RefinerError("Case evidence contains sensitive content and cannot be retained")
        if present:
            file_data[name] = data
        total += len(data)
        metadata.append({"path": name, "present": present, "content": content, "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest() if present else None})
    transcript, trace_path, trace_hash = [], None, None
    if needs_trace:
        # No installed executor can authenticate complete absence coverage yet.
        # An empty or self-asserted transcript is not proof of no actions.
        if any(check.get("type") in {"tool_command_absent", "tool_call_absent"} for check in case["checks"]):
            raise RefinerError("Tool absence cannot be independently verified: executor coverage proof unavailable")
        raw_path = os.environ.get("EVAL_TRANSCRIPT_PATH")
        if not raw_path:
            raise RefinerError("Required tool transcript is unavailable")
        trace_path = Path(raw_path)
        if not trace_path.is_file() or trace_path.is_symlink():
            raise RefinerError("Required tool transcript is unavailable or linked")
        # skill-up supplies this temporary transcript; never follow links or
        # read an arbitrary path supplied by a target or external process.
        temp_root = Path(tempfile.gettempdir()).resolve()
        if (trace_path.name != "transcript.json"
                or not re.fullmatch(r"skill-up-judge-[0-9]+", trace_path.parent.name)
                or trace_path.parent.parent.resolve() != temp_root):
            raise RefinerError("Tool transcript path is outside the pinned judge workspace")
        for component in reversed((trace_path, *trace_path.parents)):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RefinerError("Tool transcript has a linked path component")
        if trace_path.stat().st_size > _OBS_LIMIT:
            raise RefinerError("Tool transcript exceeds its size limit")
        raw = trace_path.read_bytes()
        if len(raw) > _OBS_LIMIT:
            raise RefinerError("Tool transcript exceeds its size limit")
        trace_hash = hashlib.sha256(raw).hexdigest()
        try: original = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc: raise RefinerError("Tool transcript is invalid") from exc
        transcript = _minimal_transcript(original, case["checks"], root)
        if _sensitive_observation(transcript):
            raise RefinerError("Tool evidence contains sensitive content and cannot be retained")
        # Prove the reduced transcript preserves each frozen tool verdict before discarding unrelated content.
        from .checks import check_one
        for check in case["checks"]:
            if check.get("type") in tools:
                before, _ = check_one(check, response, root, transcript=original)
                after, _ = check_one(check, response, root, transcript=transcript)
                if before != after:
                    raise RefinerError("Reduced tool transcript cannot reproduce the original verdict")
    encoded_trace = json.dumps(transcript, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if needs_trace else b""
    total += len(encoded_trace)
    if len(encoded_trace) > _OBS_LIMIT or total > 4 * _OBS_LIMIT:
        raise RefinerError("Case evidence exceeds its total size limit")
    return metadata, file_data, encoded_trace, trace_path, trace_hash


def _write_observation(case, case_path, oracle_sha, case_id, observation_dir, run_sha, schema_sha):
    import jsonschema
    from .checks import _case_response_contract, grade
    if hashlib.sha256(_OBS_SCHEMA.read_bytes()).hexdigest() != schema_sha:
        raise RefinerError("Case evidence schema changed")
    response = os.environ.get("EVAL_FINAL_MESSAGE", "")
    if _sensitive_observation(response):
        raise RefinerError("Case response contains sensitive content and cannot be retained safely")
    root = Path.cwd().resolve()
    if (root.parent != Path(tempfile.gettempdir()).resolve()
            or not re.fullmatch(r"skill-up-[0-9]+", root.name)):
        raise RefinerError("Judge workspace is outside the pinned temporary execution scope")
    metadata, file_data, transcript, trace_path, trace_hash = _observation_source(case, root, response)
    result = grade(case, response, root)
    # Do not claim that the snapshot is what was graded if inputs changed during the judge.
    again, data_again, trace_again, _, trace_hash_again = _observation_source(case, root, response)
    if metadata != again or file_data != data_again or transcript != trace_again or trace_hash != trace_hash_again:
        raise RefinerError("Case observation changed during grading")
    contract = _case_response_contract(case) if case.get("response_contract") is not None else None
    record = {"schema_version": "skill-refiner.case-observation/v1", "case_id": case_id,
              "run_sha256": run_sha, "oracle_sha256": oracle_sha,
              "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
              "contract_sha256": contract["contract_sha256"] if contract else None, "files": metadata,
              "transcript": {"required": trace_path is not None, "bytes": len(transcript),
                             "sha256": hashlib.sha256(transcript).hexdigest() if trace_path is not None else None}}
    jsonschema.Draft202012Validator(read_json(_OBS_SCHEMA)).validate(record)
    destination = Path(observation_dir)
    parent = destination.parent
    parent_stat = parent.lstat()
    if (not parent.is_dir() or stat.S_ISLNK(parent_stat.st_mode)
            or getattr(parent_stat, "st_file_attributes", 0) & 0x400
            or not destination.resolve().is_relative_to(parent.resolve())):
        raise RefinerError("Case evidence directory is unsafe")
    destination.mkdir()
    snapshot = destination / "files"; snapshot.mkdir()
    for name, data in file_data.items():
        path = _safe_evidence_path(snapshot, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if trace_path is not None:
        (destination / "transcript.json").write_bytes(transcript)
    write_json(destination / "observation.json", record)
    result["case_observation_sha256"] = hashlib.sha256((destination / "observation.json").read_bytes()).hexdigest()
    result["case_observation_run_sha256"] = run_sha
    return result


def frozen_case_grader(case_path, oracle_sha, case_id, observation_dir, run_sha, schema_sha):
    """Run one standard judge with a bounded, minimum-content observation."""
    try:
        raw = Path(case_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != oracle_sha:
            raise RefinerError("Frozen oracle hash mismatch")
        case = json.loads(raw.decode("utf-8"))
        if case.get("id") != case_id:
            raise RefinerError("Frozen oracle case identity mismatch")
        result = _write_observation(case, case_path, oracle_sha, case_id, observation_dir, run_sha, schema_sha)
    except Exception:
        result = {"case_id": case_id, "status": "ERROR", "passed": False,
                  "error": "Required case observation was unavailable, unsafe, sensitive, or inconsistent"}
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result.get("status") == "ERROR" else (0 if result["passed"] else 1)


def _replay_observation(info, case_id, response, judge_evidence):
    import jsonschema
    from .checks import _case_response_contract, check_one
    from .response_contract import validate_response
    if hashlib.sha256(_OBS_SCHEMA.read_bytes()).hexdigest() != info["observation_schema_sha256"]:
        raise RefinerError("Case observation schema changed")
    root = info["observation"]
    if not root.is_dir() or root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise RefinerError("Case observation directory missing or linked")
    meta_path = _safe_evidence_path(root, "observation.json")
    if not meta_path.is_file() or hashlib.sha256(meta_path.read_bytes()).hexdigest() != judge_evidence.get("case_observation_sha256"):
        raise RefinerError("Judge observation hash does not match")
    record = read_json(meta_path)
    jsonschema.Draft202012Validator(read_json(_OBS_SCHEMA)).validate(record)
    if (record["case_id"] != case_id or record["run_sha256"] != info["run_sha256"]
            or judge_evidence.get("case_observation_run_sha256") != info["run_sha256"]
            or record["oracle_sha256"] != info["oracle_sha256"]
            or record["contract_sha256"] != info["contract_sha256"]
            or record["response_sha256"] != hashlib.sha256(response.encode("utf-8")).hexdigest()):
        raise RefinerError("Observation does not match run, case, oracle, response or contract")
    snapshot = root / "files"
    if not snapshot.is_dir() or snapshot.is_symlink() or getattr(snapshot.lstat(), "st_file_attributes", 0) & 0x400:
        raise RefinerError("Observation file snapshot is unsafe")
    expected_paths = {}
    for check in info["checks"].values():
        kind = check.get("type", "")
        if kind.startswith("file_") or kind == "json_path_matches_file":
            name = check.get("path")
            expected_paths[name] = expected_paths.get(name, False) or kind != "file_exists"
    rows = record["files"]
    names = [row["path"] for row in rows]
    if len(names) != len(set(names)) or set(names) != set(expected_paths):
        raise RefinerError("Observation file list differs from frozen checks")
    present = set()
    for item in rows:
        path = _safe_evidence_path(snapshot, item["path"])
        if item["present"]:
            if item["content"] != expected_paths[item["path"]]:
                raise RefinerError("Observation content mode differs from frozen checks")
            if not path.is_file():
                raise RefinerError("Recorded observation file is missing")
            data = path.read_bytes()
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise RefinerError("Recorded observation file changed")
            if item["content"] and _sensitive_observation(data.decode("utf-8")):
                raise RefinerError("Recorded observation file contains sensitive data")
            if not item["content"] and data:
                raise RefinerError("Existence-only observation contains unexpected data")
            present.add(item["path"])
        elif item["content"] or item["bytes"] or item["sha256"] is not None or path.exists() or path.is_symlink():
            raise RefinerError("Absent observation file unexpectedly exists")
    found = set()
    def fail(exc):
        raise RefinerError("Cannot enumerate case observation") from exc
    for directory, subdirs, names in os.walk(snapshot, followlinks=False, onerror=fail):
        for name in subdirs:
            path = Path(directory) / name
            info_stat = path.lstat()
            if stat.S_ISLNK(info_stat.st_mode) or getattr(info_stat, "st_file_attributes", 0) & 0x400:
                raise RefinerError("Observation contains a linked directory")
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(snapshot).as_posix()
            _safe_evidence_path(snapshot, relative)
            found.add(relative)
    if found != present:
        raise RefinerError("Observation contains missing or unexpected files")
    trace_required = any(check.get("type") in {"tool_command", "tool_command_absent", "tool_call_absent", "tool_output_matches"}
                         for check in info["checks"].values())
    if record["transcript"]["required"] != trace_required:
        raise RefinerError("Observation transcript requirement differs from frozen checks")
    trace_path = root / "transcript.json"
    trace = None
    if trace_required:
        trace_path = _safe_evidence_path(root, "transcript.json")
        data = trace_path.read_bytes()
        if len(data) != record["transcript"]["bytes"] or hashlib.sha256(data).hexdigest() != record["transcript"]["sha256"]:
            raise RefinerError("Observation tool transcript changed")
        trace = json.loads(data.decode("utf-8"))
        if not isinstance(trace, list) or _sensitive_observation(trace):
            raise RefinerError("Observation tool transcript is unsafe")
    elif trace_path.exists() or record["transcript"]["bytes"] or record["transcript"]["sha256"] is not None:
        raise RefinerError("Unexpected transcript evidence")
    verdicts = {}
    for check_id, check in info["checks"].items():
        verdict, _ = check_one(check, response, snapshot, transcript=trace)
        if type(verdict) is not bool:
            raise RefinerError("Replayed assertion did not return a boolean")
        verdicts[check_id] = verdict
    if info["contract_sha256"]:
        contract = _case_response_contract(info["case"])
        if contract["contract_sha256"] != info["contract_sha256"]:
            raise RefinerError("Response contract changed")
        result = validate_response(contract, response)
        if result.get("status") not in {"pass", "fail"}:
            raise RefinerError("Response contract could not be replayed")
        verdicts["response_contract"] = result["status"] == "pass"
    return verdicts


def _verify_generated_judge(row, frozen):
    """Require hash-stable oracle/script and matching case evidence for verdicts."""
    identity = row.get("id")
    info = frozen.get(identity)
    if not info:
        return "Unrecognized frozen case"
    for key in ("oracle", "grader", "definition", "active_definition"):
        path, expected = info[key], info[key + "_sha256"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            return "Frozen oracle or judge script changed"
    if row.get("status") == "ERROR":
        return None
    raw = row.get("evidence", {})
    grading = raw.get("grading") or {}
    assertions = grading.get("assertion_results") or []
    if not isinstance(assertions, list) or len(assertions) != 1:
        return "Judge result has missing or extra script assertions"
    matching = []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        try:
            evidence = json.loads(assertion.get("evidence", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(evidence, dict) and evidence.get("case_id") == identity:
            matching.append((assertion, evidence))
    if len(matching) != 1:
        return "Judge identity evidence is missing or duplicated"
    assertion, evidence = matching[0]
    if assertion.get("text") != "script: " + str(info["grader"]):
        return "Judge script path does not match the frozen case"
    checks = evidence.get("assertions")
    if not isinstance(checks, list):
        return "Judge evidence does not contain an assertion list"
    expected = dict(info["checks"])
    if info.get("contract_sha256"):
        expected["response_contract"] = {"id": "response_contract", "type": "json_schema"}
    found = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("assertion_id"), str):
            return "Judge assertion identity is missing"
        check_id = check["assertion_id"]
        if check_id in found or check_id not in expected or check.get("check") != expected[check_id]:
            return "Judge assertion identity is duplicate, unexpected or differs from the frozen oracle"
        if type(check.get("passed")) is not bool:
            return "Judge assertion result is not a boolean"
        if check_id == "response_contract" and (not isinstance(check.get("expected_condition"), dict)
                or check["expected_condition"].get("contract_sha256") != info["contract_sha256"]):
            return "Judge response contract hash differs from the frozen contract"
        found[check_id] = check["passed"]
    if set(found) != set(expected):
        return "Judge assertion set is incomplete"
    passed = row.get("status") == "PASS"
    if (grading.get("status") != row.get("status") or assertion.get("passed") is not passed
            or evidence.get("passed") is not passed or all(found.values()) is not passed):
        return "Judge verdict is inconsistent with the case result"
    response = raw.get("response")
    if not isinstance(response, str):
        return "Actual response is missing from the upstream report"
    try:
        replayed = _replay_observation(info, identity, response, evidence)
    except Exception:
        return "Case observation is unavailable, unsafe, sensitive or cannot be independently verified"
    if replayed != found:
        return "Independent frozen assertion or response-contract verification differs from the judge"
    return None


class SkillUp:
    def __init__(self, executable, engine=None, engine_config=None, timeout=None, parallelism=2):
        if engine_config is None and engine is None:
            raise RefinerError("No evaluation engine selected; configure an executor or pass --engine explicitly")
        self.executable = locate(executable)
        self.engine = engine_config or {"name": engine}
        self.timeout = timeout
        self.parallelism = parallelism
        self.evaluations = 0
        self.max_evaluations = 50
        self.deadline = None
        self.progress_callback = None
        self.case_progress_context = None
        self.env = os.environ.copy()
        self._shim_dir = None
        if engine == "codex" and not engine_config:
            code, stdout, _, _ = run_process(["codex", "--version"], timeout=15)
            match = re.search(r"\d+\.\d+\.\S+", stdout)
            if code or not match:
                raise RefinerError("Cannot determine installed Codex CLI version")
            self.engine["version"] = match[0]
            # v0.13's rollout parser predates custom_tool_call records in recent
            # Codex. Ephemeral runs make upstream use its complete JSONL stdout
            # parser instead. Keep the real CLI/model/auth and all upstream judges.
            real_codex = shutil.which("codex")
            self._shim_dir = tempfile.TemporaryDirectory(prefix="refiner-codex-")
            shim = Path(self._shim_dir.name) / "codex"
            shim.write_text("#!/bin/sh\nif [ \"$1\" = exec ]; then\n shift\n exec " +
                            shlex.quote(real_codex) + " exec --ephemeral \"$@\"\nfi\nexec " +
                            shlex.quote(real_codex) + " \"$@\"\n")
            shim.chmod(0o755)
            self.env["PATH"] = str(shim.parent) + os.pathsep + self.env.get("PATH", "")

    def _call_timeout(self, operation):
        limits = []
        if self.timeout is not None:
            if self.timeout <= 0:
                raise RefinerError("Per-call fallback timeout must be positive when specified")
            limits.append(self.timeout)
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RefinerError(f"Wall-clock budget exhausted before {operation}")
            limits.append(remaining)
        return min(limits) if limits else None

    def evaluate(self, target, cases, run_dir, *, selected=None):
        """Hold the cross-process judge lock for target preparation through cleanup."""
        with local_judge_lock(self.deadline):
            return self._evaluate_locked(target, cases, run_dir, selected=selected)

    def _evaluate_locked(self, target, cases, run_dir, *, selected=None):
        target, run_dir = Path(target).resolve(), Path(run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        cases = [c for c in cases if selected is None or c["id"] in selected]
        if not cases:
            raise RefinerError("Refusing to run an empty test selection")
        ids = [case.get("id") for case in cases]
        if (any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) for value in ids)
                or len(set(ids)) != len(ids)):
            raise RefinerError("Generated evaluation case IDs must be unique, safe frozen IDs")
        # Missing future target files are target failures, not infrastructure
        # errors. Do not invoke an executor for a case that cannot yet run;
        # preserve all other cases and allow the refiner to repair the target.
        from .planned_dependencies import inspect_target_dependencies
        dependency_results = {case['id']: inspect_target_dependencies(case, target)
                              for case in cases if case.get('planned_target_dependencies')}
        missing = {case_id: rows for case_id, rows in dependency_results.items()
                   if any(row['status'] == 'not_ready' for row in rows)}
        if missing:
            # A not-ready check is still a charged case evaluation even though
            # it does not start the target executor; this keeps cache and
            # cumulative accounting consistent on resume.
            if self.evaluations + len(missing) > self.max_evaluations:
                raise RefinerError("Target-evaluation budget cannot cover planned dependency checks")
            self.evaluations += len(missing)
            ready_cases = [case for case in cases if case['id'] not in missing]
            if ready_cases:
                result = self._evaluate_locked(target, ready_cases, run_dir)
            else:
                result = {'passed': False, 'cases': [], 'seconds': 0, 'engine': self.engine,
                          'upstream_exit_code': None, 'requested_parallelism': self.parallelism,
                          'effective_parallelism': 0,
                          'execution_note': 'No case executed: planned target dependencies are not ready.'}
            for case_id in ids:
                if case_id not in missing:
                    continue
                current = inspect_target_dependencies(next(case for case in cases if case['id'] == case_id), target)
                if current != missing[case_id]:
                    raise RefinerError("Planned target dependency changed during evaluation")
                result['cases'].append({'id': case_id, 'status': 'FAIL', 'passed': False,
                    'evidence': {'case_id': case_id, 'status': 'FAIL', 'executed': False,
                                 'reason': 'Planned target dependency is not ready.',
                                 'planned_target_dependencies': current}})
            by_id = {row['id']: row for row in result['cases']}
            if len(by_id) != len(ids) or set(by_id) != set(ids):
                raise RefinerError("Planned dependency results do not match the frozen cases")
            result['cases'] = [by_id[case_id] for case_id in ids]
            result['passed'] = False
            write_json(run_dir / "summary.json", result)
            progress = _CaseProgress(list(missing), 'configured', self.case_progress_context)
            progress.finalize([by_id[case_id] for case_id in ids if case_id in missing],
                              evidence_ref=str(run_dir / "summary.json"))
            return result
        engine_label = (self.engine.get("name", self.engine.get("model", "configured"))
                        if isinstance(self.engine, dict) else "configured")
        case_progress = _CaseProgress(ids, engine_label, self.case_progress_context)
        frozen = {}
        evals = target / "evals"
        generated = evals / ".refiner-generated"
        config_path = evals / "refiner-eval.yaml"
        # Never remove a pre-existing or unknown owner's content.
        if generated.exists() or generated.is_symlink() or config_path.exists() or config_path.is_symlink():
            raise RefinerError("Target already contains reserved refiner evaluation files")
        if evals.exists():
            info = evals.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                    or not evals.is_dir() or not evals.resolve().is_relative_to(target)):
                raise RefinerError("Evaluation directory is linked, unsafe or outside the target")
        else:
            evals.mkdir(parents=True)
        generated.mkdir()
        generated_stat = generated.lstat()
        generated_identity = (generated_stat.st_dev, generated_stat.st_ino)
        config_owned = False
        config_identity = None
        try:
            case_paths = []
            contract_hashes = {}
            for case in cases:
                if case.get("response_contract") is not None:
                    from .checks import _case_response_contract
                    contract_hashes[case["id"]] = _case_response_contract(case)["contract_sha256"]
                frozen_path = run_dir / f"{case['id']}.oracle.json"
                write_json(frozen_path, case)
                grader = run_dir / f"{case['id']}.judge.sh"
                module_root = str(Path(__file__).resolve().parent.parent)
                oracle_sha = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
                observation = run_dir / (case["id"] + ".observation")
                run_sha = hashlib.sha256(str(run_dir).encode("utf-8")).hexdigest()
                schema_sha = hashlib.sha256(_OBS_SCHEMA.read_bytes()).hexdigest()
                pycode = "from refiner.skillup import frozen_case_grader; import sys; sys.exit(frozen_case_grader(*sys.argv[1:]))"
                grader_args = " ".join(shlex.quote(value) for value in
                    (str(frozen_path), oracle_sha, case["id"], str(observation), run_sha, schema_sha))
                grader.write_text("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(module_root) +
                                  "\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(pycode) +
                                  " " + grader_args + "\n", encoding="utf-8")
                grader.chmod(0o755)
                checks = read_json(frozen_path).get("checks")
                if not isinstance(checks, list) or not checks or any(not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"] for check in checks):
                    raise RefinerError("Frozen case requires identifiable checks")
                check_ids = [check["id"] for check in checks]
                if len(check_ids) != len(set(check_ids)) or (case.get("response_contract") is not None and "response_contract" in check_ids):
                    raise RefinerError("Frozen case has duplicate or reserved assertion IDs")
                frozen[case["id"]] = {"oracle": frozen_path, "oracle_sha256": hashlib.sha256(frozen_path.read_bytes()).hexdigest(),
                                      "grader": grader, "grader_sha256": hashlib.sha256(grader.read_bytes()).hexdigest(),
                                      "checks": {check["id"]: check for check in checks},
                                      "case": read_json(frozen_path), "observation": observation,
                                      "run_sha256": run_sha, "observation_schema_sha256": schema_sha,
                                      "contract_sha256": contract_hashes.get(case["id"])}
                native = {"id": case["id"], "title": case["id"], "input": {"prompt": case["prompt"]},
                          "context": {"files": {**case["files"], **(
                              {"refiner-python-probe.py": Path(__file__).with_name("python_probe.py").read_text(encoding="utf-8")}
                              if "python_probe" in case.get("helpers", []) else {})}},
                          "constraints": {"timeout_seconds": self.timeout},
                          "judge": {"type": "script", "script_path": str(grader),
                                    "timeout_seconds": self.timeout}}
                path = generated / f"{case['id']}.yaml"
                write_json(path, native)
                definition_copy = run_dir / f"{case['id']}.case.json"
                shutil.copyfile(path, definition_copy)
                definition_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                frozen[case["id"]]["definition"] = definition_copy
                frozen[case["id"]]["definition_sha256"] = definition_sha
                frozen[case["id"]]["active_definition"] = path
                frozen[case["id"]]["active_definition_sha256"] = definition_sha
                case_paths.append(str(path.relative_to(target)))
            config = {"schema_version": "v1alpha1", "environment": {"type": "none"},
                      "skills": [{"source": "local_path", "path": "."}], "engine": self.engine,
                      "cases": {"files": case_paths, "parallelism": 1,
                                "defaults": {"timeout_seconds": self.timeout}},
                      "benchmark": {"enabled": False}, "report": {"formats": ["json"]}}
            with config_path.open("x", encoding="utf-8", newline="\n") as stream:
                config_owned = True
                json.dump(config, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                info = os.fstat(stream.fileno())
                config_identity = (info.st_dev, info.st_ino)
            config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
            code, _, _, _ = run_process([self.executable, "validate", str(config_path)],
                                         timeout=self._call_timeout("evaluation validation"),
                                         log=run_dir / "validate.log")
            if code:
                raise RefinerError(f"skill-up rejected generated evals; see {run_dir / 'validate.log'}")
            result = self._execute(config_path, run_dir, len(cases), required_contract_hashes=contract_hashes,
                                   case_progress=case_progress, lock_held=True)
            if (not config_path.is_file() or config_path.is_symlink()
                    or hashlib.sha256(config_path.read_bytes()).hexdigest() != config_sha256):
                raise RefinerError("Frozen skill-up configuration changed during evaluation")
            results = result["cases"]
            if {r["id"] for r in results} != set(ids) or len(results) != len(ids):
                raise RefinerError("skill-up returned missing, duplicated or unexpected cases")
            probe_start = time.monotonic()
            probe_count = [0]
            def count_probe(): probe_count[0] += 1
            for row in results:
                issue = _verify_generated_judge(row, frozen)
                info = frozen[row["id"]]
                row["verification"] = {"oracle_sha256": info["oracle_sha256"],
                    "grader_sha256": info["grader_sha256"], "definition_sha256": info["definition_sha256"],
                    "observation_schema_sha256": info["observation_schema_sha256"],
                    "contract_sha256": info["contract_sha256"], "run_sha256": info["run_sha256"],
                    "independent_replay": "failed" if issue else ("not_available" if row["status"] == "ERROR" else "passed")}
                if issue:
                    row["status"], row["passed"] = "ERROR", False
                    row["evidence"]["error"] = issue
                elif row['status'] != 'ERROR' and info['case'].get('planned_target_dependencies'):
                    from .docker_target_probe import validate_case_dependencies
                    try:
                        probes = validate_case_dependencies(info['case'], target, row['evidence'].get('response'),
                                                            deadline=self.deadline, on_probe=count_probe,
                                                            probe_budget=getattr(self, 'probe_budget', None))
                        row['planned_dependency_probes'] = probes
                        if not all(item['probe']['passed'] for item in probes):
                            row['status'], row['passed'] = 'FAIL', False
                            row['evidence']['planned_dependency_error'] = 'Authoritative validator or its controls failed'
                    except Exception as exc:
                        row['status'], row['passed'] = 'ERROR', False
                        row['evidence']['planned_dependency_error'] = 'Authoritative validator unavailable: ' + str(exc)
            result["validator_probe_calls"] = probe_count[0]
            result["validator_probe_seconds"] = round(time.monotonic() - probe_start, 3)
            result["seconds"] += result["validator_probe_seconds"]
            result["passed"] = all(row["passed"] for row in results)
            write_json(run_dir / "summary.json", result)
            case_progress.finalize(results, evidence_ref=str(run_dir / "summary.json"))
            counts = {status: sum(row["status"] == status for row in results) for status in ("PASS", "FAIL", "ERROR")}
            print(json.dumps({"stage": "evaluation", "status": "completed", "seconds": result["seconds"],
                              "case_count": len(results), **counts}, ensure_ascii=False), flush=True)
            return result
        except BaseException:
            case_progress.abort()
            raise
        finally:
            cleanup_issue = None
            if config_owned:
                if config_path.exists() or config_path.is_symlink():
                    info = config_path.lstat()
                    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                            or (info.st_dev, info.st_ino) != config_identity):
                        cleanup_issue = "Generated evaluation configuration was replaced"
                    else:
                        config_path.unlink()
                else:
                    cleanup_issue = "Generated evaluation configuration disappeared"
            if generated.exists() or generated.is_symlink():
                info = generated.lstat()
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or (info.st_dev, info.st_ino) != generated_identity
                        or not generated.resolve().is_relative_to(target)):
                    cleanup_issue = "Generated evaluation directory was replaced or escaped the target"
                else:
                    shutil.rmtree(generated)
            else:
                cleanup_issue = "Generated evaluation directory disappeared"
            if cleanup_issue:
                raise RefinerError(cleanup_issue)

    def _execute(self, config_path, run_dir, case_count, *, required_contract_hashes=None, case_progress=None, lock_held=False):
        if self.evaluations + case_count > self.max_evaluations:
            raise RefinerError("Target-evaluation budget exhausted")
        self.evaluations += case_count
        timeout = self._call_timeout("evaluation")
        with nullcontext() if lock_held else local_judge_lock(self.deadline):
            timeout = self._call_timeout("evaluation after waiting for judge lock")
            engine_label = (self.engine.get("name", self.engine.get("model", "configured"))
                            if isinstance(self.engine, dict) else "configured")
            print(json.dumps({"stage": "evaluation", "status": "running", "engine": engine_label,
                              "case_count": case_count, "case_evaluations_reserved": case_count,
                              "max_evaluations": self.max_evaluations,
                              "timeout_seconds": round(timeout, 3) if timeout is not None else None},
                             ensure_ascii=False), flush=True)
            def heartbeat(elapsed, remaining):
                if self.progress_callback:
                    self.progress_callback(elapsed, remaining, case_count, engine_label, self.evaluations)
                print(json.dumps({"stage": "evaluation", "status": "still_running", "engine": engine_label,
                                  "elapsed_seconds": elapsed, "remaining_timeout_seconds": remaining,
                                  "case_count": case_count, "case_evaluations_reserved": case_count,
                                  "max_evaluations": self.max_evaluations}, ensure_ascii=False), flush=True)
            code, _, _, duration = run_process(
                [self.executable, "run", str(config_path), "--output-dir", str(run_dir / "upstream")],
                timeout=timeout, log=run_dir / "skill-up.log", env=self.env,
                on_progress=heartbeat, on_output=case_progress.feed if case_progress else None, progress_interval=30)
            if case_progress:
                case_progress.finish()
        reports = sorted((run_dir / "upstream").glob("iteration-*/result.json"))
        if not reports:
            raise RefinerError(f"skill-up produced no result.json (exit {code}); see {run_dir / 'skill-up.log'}")
        raw = read_json(reports[-1])
        write_json(run_dir / "upstream-result.json", raw)
        rows = raw.get("case_results", [])
        if not rows:
            raise RefinerError("Unknown skill-up result schema or zero cases")
        results = []
        for row in rows:
            status = row.get("status", "ERROR").upper()
            if status not in {"PASS", "FAIL", "ERROR"}:
                status = "ERROR"
            # Detect misrouted upstream script judges independently of verdict.
            required_sha = (required_contract_hashes or {}).get(row.get("case_id"))
            contract_evidence_found = False
            for assertion in (row.get("grading") or {}).get("assertion_results", []):
                try:
                    evidence = json.loads(assertion.get("evidence", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(evidence, dict) and evidence.get("case_id") == row.get("case_id") and required_sha:
                    for item in evidence.get("assertions", []):
                        if (isinstance(item, dict) and item.get("assertion_id") == "response_contract"
                                and isinstance(item.get("expected_condition"), dict)
                                and item["expected_condition"].get("contract_sha256") == required_sha):
                            contract_evidence_found = True
                            if status == "PASS" and item.get("passed") is not True:
                                status = "ERROR"
                                row["error"] = "Passing case lacks a passing response contract assertion"
                if isinstance(evidence, dict) and evidence.get("status") == "ERROR":
                    status = "ERROR"
                    row["error"] = "Frozen oracle or response contract could not be validated"
                if isinstance(evidence, dict) and "case_id" in evidence and evidence["case_id"] != row.get("case_id"):
                    status = "ERROR"
                    row["error"] = "Judge case identity mismatch; result cannot be trusted"
            if required_sha and not contract_evidence_found:
                status = "ERROR"
                row["error"] = "Frozen response contract judge evidence is missing or mismatched"
            results.append({"id": row.get("case_id"), "status": status,
                            "evidence_directory": str((run_dir / "upstream").resolve()),
                            "evidence": row, "passed": status == "PASS"})
        if code and all(r["passed"] for r in results):
            raise RefinerError("skill-up exited nonzero despite all cases passing")
        summary = {"passed": all(r["passed"] for r in results), "cases": results,
                "seconds": duration, "engine": self.engine, "upstream_exit_code": code,
                "requested_parallelism": self.parallelism, "effective_parallelism": 1,
                "execution_note": "Pinned skill-up local script judges serialized to prevent temporary-directory collisions",
                "upstream_report": str(reports[-1])}
        if case_progress is None:
            counts = {status: sum(row["status"] == status for row in results) for status in ("PASS", "FAIL", "ERROR")}
            print(json.dumps({"stage": "evaluation", "status": "completed", "seconds": duration,
                              "case_count": len(results), **counts}, ensure_ascii=False), flush=True)
        return summary

    def evaluate_existing(self, target, run_dir):
        """Protect native suite preparation and execution with the same cross-process lock."""
        with local_judge_lock(self.deadline):
            return self._evaluate_existing_locked(target, run_dir)

    def _evaluate_existing_locked(self, target, run_dir):
        """Preserve and execute native eval.yaml; never claim existing tests ran if unsupported."""
        import yaml
        target, run_dir = Path(target), Path(run_dir)
        config = target / "evals" / "eval.yaml"
        if not config.exists():
            if (target / "evals" / "evals.json").exists():
                raise RefinerError("Existing Anthropic evals.json must first be imported with skill-up import")
            return None
        native = yaml.safe_load(config.read_text(encoding="utf-8"))
        if not isinstance(native, dict) or native.get("environment", {}).get("type", "none") != "none":
            raise RefinerError("v0.1 existing eval suites must use environment.type=none")
        native["engine"] = self.engine
        native["benchmark"] = {"enabled": False}
        native["report"] = {"formats": ["json"]}
        native["cases"]["parallelism"] = 1
        generated = target / "evals" / "refiner-existing.yaml"
        if generated.exists():
            raise RefinerError("Reserved evals/refiner-existing.yaml already exists")
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(generated, native)
        try:
            code, _, _, _ = run_process([self.executable, "validate", str(generated)],
                                        timeout=self._call_timeout("existing evaluation validation"),
                                        log=run_dir / "validate.log")
            if code:
                raise RefinerError(f"Existing native suite validation failed: {run_dir / 'validate.log'}")
            result = self._execute(generated, run_dir, len(native["cases"]["files"]), lock_held=True)
            for row in result["cases"]:
                row["id"] = "existing:" + row["id"]
            write_json(run_dir / "summary.json", result)
            return result
        finally:
            generated.unlink(missing_ok=True)


