from __future__ import annotations

from contextlib import contextmanager
import tempfile
import hashlib
import codecs
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import re
import subprocess
import threading
import time
from queue import Empty, Queue


class RefinerError(Exception):
    pass


class PlanGateError(RefinerError):
    """The generated plan or its controls/review failed before Skill evaluation."""


class PlanValidationError(RefinerError):
    """A generated plan failed self-check, optionally with completed control evidence."""

    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


class PlanControlError(PlanValidationError):
    """A complete plan-control pass ran but one or more controls failed."""


class Cancelled(RefinerError):
    pass


IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".tools", ".refiner-runs"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json_target_identity(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _windows_delete_share_conflict(paths):
    """Probe without mutation; WinError 5 alone is not proof of a share lock."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    invalid = wintypes.HANDLE(-1).value
    for path in paths:
        # DELETE access tests the share mode required by MoveFileEx/replace.
        # Open existing, share all; this does not write, delete or alter a file.
        handle = create(str(path), 0x00010000, 0x00000007, None, 3, 0x80, None)
        if handle == invalid:
            if ctypes.get_last_error() in {32, 33}:
                return True
        else:
            close(handle)
    return False



_json_mutex_table_lock = threading.Lock()
_json_mutex_table = {}


@contextmanager
def _json_target_lock(path):
    """Serialize cooperating Windows writers across processes for one target."""
    key = os.path.normcase(os.path.abspath(str(path)))
    # Extended paths must name the same mutex as their ordinary Windows form.
    if os.name == "nt" and key.startswith("\\\\?\\unc\\"):
        key = "\\\\" + key[8:]
    elif os.name == "nt" and key.startswith("\\\\?\\"):
        key = key[4:]
    key = os.path.normcase(os.path.realpath(key))
    if os.name != "nt":
        with _json_mutex_table_lock:
            lock = _json_mutex_table.setdefault(key, threading.Lock())
        if not lock.acquire(timeout=1.0):
            raise RefinerError("Timed out waiting for JSON target writer")
        try:
            yield
        finally:
            lock.release()
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    release = kernel.ReleaseMutex
    release.argtypes = [wintypes.HANDLE]
    release.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    name = "Local\\refiner-json-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    handle = create(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = wait(handle, 1000)
        if result == 0x102:
            raise RefinerError("Timed out waiting for JSON target writer")
        if result not in (0, 0x80):
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    finally:
        if acquired:
            release(handle)
        close(handle)

def _replace_json_atomically(tmp, target, baseline):
    """Bound sharing retries; refuse to overwrite a changed target after waiting."""
    deadline = time.monotonic() + 1.0
    delay = 0.025
    while True:
        if _json_target_identity(target) != baseline:
            raise RefinerError("Concurrent JSON target change; refusing stale atomic replacement")
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            retryable = os.name == "nt" and (code in {32, 33} or
                        (code == 5 and _windows_delete_share_conflict((tmp, target))))
            remaining = deadline - time.monotonic()
            if not retryable or remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.2)


def write_json(path, value):
    path = Path(path)
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline = _json_target_identity(path)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        with _json_target_lock(path):
            _replace_json_atomically(tmp, path, baseline)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            # A failed cleanup must not hide the original write/replace failure.
            # If the atomic replacement succeeded the temp name no longer exists.
            pass



def relative_path(root, value):
    root = Path(root).resolve()
    p = Path(value)
    if p.is_absolute() or not p.parts or ".." in p.parts or "\\" in str(value):
        raise RefinerError(f"Unsafe relative path: {value}")
    target = root / p
    if not target.resolve().is_relative_to(root):
        raise RefinerError(f"Path escapes workspace: {value}")
    return target


def files(root):
    root = Path(root)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in IGNORED or ".before-" in part for part in rel.parts):
            continue
        if p.is_symlink():
            raise RefinerError(f"Symlink requires explicit materialization first: {p}")
        if p.is_file():
            yield p


def manifest(root):
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files(root)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise RefinerError("Snapshot must be a new directory outside the target Skill")
    destination.mkdir(parents=True)
    for path in files(source):
        dest = destination / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def inventory(root, max_bytes=250_000, priority_paths=(), exclude_paths=(".runtime/venv",)):
    root = Path(root)
    records, used = [], 0
    priority_order = list(dict.fromkeys(Path(item).as_posix() for item in priority_paths))
    priority = {item: index for index, item in enumerate(priority_order)}
    excluded = {Path(item).as_posix().rstrip("/") for item in exclude_paths}

    def is_excluded(path):
        relative = path.relative_to(root).as_posix()
        return any(relative == prefix or relative.startswith(prefix + "/") for prefix in excluded)

    ordered = sorted((item for item in files(root) if not is_excluded(item)),
                     key=lambda item: (str(item.relative_to(root)) not in priority,
                                       priority.get(str(item.relative_to(root)), 0),
                                       str(item.relative_to(root))))
    for path in ordered:
        size = path.stat().st_size
        raw = path.read_bytes()
        record = {"path": str(path.relative_to(root)), "bytes": size,
                  "sha256": hashlib.sha256(raw).hexdigest()}
        try:
            text = raw.decode("utf-8")
        except (UnicodeError, OSError):
            record["omitted"] = "binary"
        else:
            if used + size > max_bytes:
                record["omitted"] = "context limit; unresolved dependency if required"
            else:
                record["content"] = text
                used += size
        records.append(record)
    return records


def kill_process_tree(pid):
    """Nested adapters may create their own process groups; stop them too."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        return
    descendants = {pid}
    try:
        listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5, check=True)
        pairs = [tuple(map(int, line.split())) for line in listing.stdout.splitlines() if len(line.split()) == 2]
        while True:
            expanded = descendants | {child for child, parent in pairs if parent in descendants}
            if expanded == descendants:
                break
            descendants = expanded
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for child in descendants - {pid}:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _safe_process_argument(value):
    """Keep diagnostic argv useful without copying common credential forms."""
    value = str(value)
    value = re.sub(r"(?:sk-|crsr_)[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    value = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
    if re.search(r"(?i)(api[_-]?key|token|password|secret|authorization)\s*=", value):
        return value.split("=", 1)[0] + "=[REDACTED]"
    return value if len(value) <= 1000 else "[OMITTED:LARGE_ARGUMENT]"


class _ProcessJournal:
    """Durable local output and process identity, including before a hard interruption."""

    _validator = None

    def __init__(self, log, argv, cwd, input_text, timeout):
        self.path = Path(log) if log else None
        self.started = time.monotonic()
        self.last_saved = self.started
        self.data = None
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stdout_path = self.path.with_suffix(".stdout.log")
        self.stderr_path = self.path.with_suffix(".stderr.log")
        self.record_path = self.path.with_suffix(".execution.json")
        for path in (self.path, self.stdout_path, self.stderr_path):
            path.write_text("", encoding="utf-8")
        safe_argv, hide_next = [], False
        for argument in argv:
            text = str(argument)
            safe_argv.append("[REDACTED]" if hide_next else _safe_process_argument(text))
            hide_next = bool(re.fullmatch(r"(?i)--?(api[_-]?key|token|password|secret|authorization)", text))
        self.data = {
            "schema_version": "skill-refiner.process-execution/v1",
            "argv": safe_argv, "cwd": str(Path(cwd).resolve()) if cwd else None,
            "pid": None, "status": "starting", "exit_code": None,
            "started_at": self._now(), "finished_at": None, "elapsed_seconds": 0.0,
            "timeout_seconds": timeout, "input_bytes": len((input_text or "").encode("utf-8")),
            "stdout_bytes": 0, "stderr_bytes": 0, "last_output_at": None,
            "stdout_path": self.stdout_path.name, "stderr_path": self.stderr_path.name,
            "error": None,
        }
        self._save()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _save(self):
        if self.data is not None:
            self.data["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
            if type(self)._validator is None:
                import jsonschema
                schema_path = Path(__file__).resolve().parents[2] / "schemas/v1/process-execution.schema.json"
                schema = read_json(schema_path)
                jsonschema.Draft202012Validator.check_schema(schema)
                type(self)._validator = jsonschema.Draft202012Validator(schema)
            type(self)._validator.validate(self.data)
            write_json(self.record_path, self.data)
            self.last_saved = time.monotonic()

    def running(self, pid):
        if self.data is not None:
            self.data.update(status="running", pid=pid)
            self._save()

    def output(self, stream, chunk):
        if self.data is None:
            return
        path = self.stdout_path if stream == "stdout" else self.stderr_path
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(chunk)
            handle.flush()
        # Preserve a live combined log too. Normal completion rewrites its legacy
        # stdout/STDERR format; sidecars retain the complete per-stream bytes.
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(f"[{stream}] " + chunk)
            handle.flush()
        self.data[stream + "_bytes"] += len(chunk.encode("utf-8"))
        self.data["last_output_at"] = self._now()
        self._save()

    def heartbeat(self):
        if self.data is not None and time.monotonic() - self.last_saved >= 1:
            self._save()

    def finish(self, status, exit_code, error=None):
        if self.data is not None:
            self.data.update(status=status, exit_code=exit_code,
                             finished_at=None if status == "termination_unconfirmed" else self._now(),
                             error=_safe_process_argument(error) if error else None)
            self._save()


def _run_streaming_process(argv, *, cwd=None, input_text=None, timeout=None, log=None, env=None,
                           on_progress=None, on_output=None, progress_interval=30):
    started = time.monotonic()
    journal = _ProcessJournal(log, argv, cwd, input_text, timeout)
    try:
        proc = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace",
                                bufsize=1, start_new_session=True, env=env)
    except Exception as exc:
        journal.finish("launch_error", None, str(exc))
        raise
    journal.running(proc.pid)
    output_queue = Queue()
    streams_done = set()
    stdout_parts, stderr_parts = [], []
    callback_pending = {"stdout": "", "stderr": ""}

    def pump(stream_name, stream):
        try:
            # read1 emits flushed partial lines immediately. Keep JSONL framing
            # for callbacks below, while raw output is persisted without waiting
            # for a newline or process exit.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                chunk = stream.buffer.read1(4096)
                if not chunk:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        output_queue.put((stream_name, tail))
                    break
                text = decoder.decode(chunk)
                if text:
                    output_queue.put((stream_name, text))
        finally:
            try:
                stream.close()
            except OSError:
                pass
            output_queue.put((stream_name, None))

    def deliver(stream_name, chunk):
        if chunk is None:
            streams_done.add(stream_name)
        else:
            (stdout_parts if stream_name == "stdout" else stderr_parts).append(chunk)
            journal.output(stream_name, chunk)
            if on_output:
                callback_pending[stream_name] += chunk
        if on_output:
            while "\n" in callback_pending[stream_name]:
                line, callback_pending[stream_name] = callback_pending[stream_name].split("\n", 1)
                try:
                    on_output(stream_name, line.removesuffix("\r") + "\n")
                except Exception:
                    pass
            if chunk is None and callback_pending[stream_name]:
                try:
                    on_output(stream_name, callback_pending[stream_name].replace("\r", "\n"))
                except Exception:
                    pass
                callback_pending[stream_name] = ""

    def feed_stdin():
        try:
            if input_text is not None:
                proc.stdin.write(input_text)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    readers = [threading.Thread(target=pump, args=("stdout", proc.stdout), daemon=True),
               threading.Thread(target=pump, args=("stderr", proc.stderr), daemon=True)]
    writer = threading.Thread(target=feed_stdin, daemon=True)
    for thread in readers:
        thread.start()
    writer.start()

    deadline = None if timeout is None else started + timeout
    next_progress = started + max(1, progress_interval)
    timed_out = False
    interrupted = False
    try:
        while True:
            now = time.monotonic()
            remaining = None if deadline is None else max(0.0, deadline - now)
            wait_for = max(0.05, next_progress - now)
            wait_for = min(wait_for, 0.2)  # Poll process exit even when no output is emitted.
            if remaining is not None:
                wait_for = min(wait_for, remaining)
            try:
                stream_name, chunk = output_queue.get(timeout=wait_for)
                deliver(stream_name, chunk)
            except Empty:
                pass

            now = time.monotonic()
            journal.heartbeat()
            if now >= next_progress:
                if on_progress:
                    try:
                        on_progress(round(now - started, 1),
                                    None if deadline is None else round(max(0.0, deadline - now), 1))
                    except Exception:
                        pass
                next_progress = now + max(1, progress_interval)
            if deadline is not None and now >= deadline:
                timed_out = True
                break
            if proc.poll() is not None and len(streams_done) == 2 and output_queue.empty():
                break
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        # A failed evidence write must not leave an unobserved child running.
        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            journal.finish("execution_error" if proc.returncode is not None else "termination_unconfirmed",
                           proc.returncode, str(exc))
        except OSError:
            pass
        raise

    if timed_out or interrupted:
        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for thread in readers + [writer]:
            thread.join(timeout=1)
        while True:
            try:
                stream_name, chunk = output_queue.get_nowait()
            except Empty:
                break
            deliver(stream_name, chunk)
    else:
        proc.wait()
        writer.join(timeout=1)
        for thread in readers:
            thread.join(timeout=1)

    raw_stdout, raw_stderr = "".join(stdout_parts), "".join(stderr_parts)
    # Keep the public text API's universal-newline behavior. Sidecars preserve
    # decoded stream bytes, including CRLF, independently of platform text I/O.
    stdout = raw_stdout.replace("\r\n", "\n").replace("\r", "\n")
    stderr = raw_stderr.replace("\r\n", "\n").replace("\r", "\n")
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(raw_stdout + "\nSTDERR\n" + raw_stderr, encoding="utf-8", newline="")
    status = ("termination_unconfirmed" if proc.returncode is None else
              "cancelled" if interrupted else "timed_out" if timed_out else
              "completed" if proc.returncode == 0 else "execution_error")
    journal.finish(status, proc.returncode,
                   "Process exit could not be confirmed; external termination evidence is required"
                   if status == "termination_unconfirmed" else None)
    if interrupted:
        raise Cancelled(f"Process interrupted: {argv[0]}")
    if timed_out:
        raise RefinerError(f"Process timed out after {timeout}s: {argv[0]}")
    return proc.returncode, stdout, stderr, round(time.monotonic() - started, 3)


def run_process(argv, *, cwd=None, input_text=None, timeout=None, log=None, env=None,
                on_progress=None, on_output=None, progress_interval=30):
    """Run a process; stream output when requested and only kill it for an explicit timeout."""
    if log is not None or timeout is None or on_output is not None:
        return _run_streaming_process(argv, cwd=cwd, input_text=input_text, timeout=timeout,
                                      log=log, env=env, on_progress=on_progress,
                                      on_output=on_output, progress_interval=progress_interval)
    started = time.monotonic()
    proc = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace",
                            start_new_session=True, env=env)
    deadline = started + timeout
    pending_input = input_text
    try:
        while True:
            remaining = max(0, deadline - time.monotonic())
            wait_for = min(remaining, max(1, progress_interval)) if on_progress else remaining
            try:
                stdout, stderr = proc.communicate(pending_input, timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                elapsed = time.monotonic() - started
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                if on_progress:
                    try:
                        on_progress(round(elapsed, 1), round(remaining, 1))
                    except Exception:
                        # Progress reporting must never affect or leak the child process.
                        pass
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        kill_process_tree(proc.pid)
        try:
            # A descendant can keep the inherited stdout/stderr pipes open even
            # after the parent was terminated. Never let timeout cleanup wait on
            # those pipes indefinitely.
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as cleanup_exc:
            stdout = cleanup_exc.output or getattr(exc, "output", None) or ""
            stderr = cleanup_exc.stderr or getattr(exc, "stderr", None) or ""
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        if log:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            Path(log).write_text(stdout + "\n" + stderr, encoding="utf-8")
        if isinstance(exc, KeyboardInterrupt):
            raise Cancelled(f"Process interrupted: {argv[0]}") from exc
        raise RefinerError(f"Process timed out after {timeout}s: {argv[0]}") from exc
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(stdout + "\nSTDERR\n" + stderr, encoding="utf-8")
    return proc.returncode, stdout, stderr, round(time.monotonic() - started, 3)

