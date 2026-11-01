"""Bounded Docker probe for a planned Python target asset.

A probe shows a controller-invoked CLI execution only. It is not evidence that
an agent used the CLI or that the agent had no other host-side tool routes.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from .common import RefinerError
from .planned_dependencies import inspect_target_dependencies

IMAGE = 'sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e'
LIMIT = 65536

def _sha(b):
    return hashlib.sha256(b).hexdigest()

CLEANUP_GRACE_SECONDS = 10.0

class _CleanupWindow:
    """One wall-clock grace period shared by all process/container cleanup."""
    def __init__(self, deadline, grace=None):
        self.business_deadline = deadline
        self.grace = CLEANUP_GRACE_SECONDS if grace is None else grace
        self.end = None
        self.incomplete = False
        self.reason = None
        self._lock = threading.Lock()

    def local_termination_limit(self, reserve_container):
        with self._lock:
            now=time.monotonic()
            if self.end is None: self.end=min(self.business_deadline+self.grace,now+self.grace)
            reserve=min(5.0,self.grace/2) if reserve_container else 0
            return min(1.0,max(0.0,self.end-now-reserve))

    def call(self, label, operation, *, force=False, max_seconds=None):
        with self._lock:
            now = time.monotonic()
            if self.end is None:
                self.end = min(self.business_deadline + self.grace, now + self.grace)
            remaining = self.end - now
            if (self.incomplete and not force) or remaining <= 0:
                self.incomplete = True
                self.reason = self.reason or ('cleanup deadline exhausted before ' + label)
                return False
        if max_seconds is not None: remaining=min(remaining,max_seconds)
        operation_deadline = time.monotonic()+remaining
        state = {'error': None, 'skipped': False}
        def invoke():
            if time.monotonic() >= operation_deadline:
                state['skipped'] = True
                return
            try: operation(max(0.0, operation_deadline-time.monotonic()))
            except Exception as exc: state['error'] = exc
        worker=threading.Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(max(0.0, operation_deadline-time.monotonic()))
        if worker.is_alive() or state['skipped'] or state['error'] is not None:
            with self._lock:
                self.incomplete = True
                self.reason = self.reason or ('cleanup incomplete at ' + label)
            return False
        return True


def _run_bounded(argv, stdin, timeout, *, deadline=None, on_started=None, on_cleanup=None, cleanup=None):
    """Drain bounded output; all cleanup shares one absolute grace deadline."""
    deadline = deadline if deadline is not None else time.monotonic()+timeout
    remaining = deadline-time.monotonic()
    if remaining <= 0: raise RefinerError('Validator deadline exhausted immediately before Docker launch')
    timeout = min(timeout, remaining)
    cleanup = cleanup or _CleanupWindow(deadline)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if on_started is not None: on_started()
    chunks = [bytearray(), bytearray()]
    overflow = threading.Event()
    threads=[]
    writer=None
    error=None
    code=None
    try:
        if time.monotonic() >= deadline:
            raise RefinerError('Validator deadline exhausted during Docker launch')
        def drain(stream, data):
            while True:
                block = stream.read(8192)
                if not block: return
                if len(data) + len(block) > LIMIT:
                    overflow.set()
                    cleanup.call('terminate oversized output',lambda _:proc.kill(),
                                 max_seconds=cleanup.local_termination_limit(on_cleanup is not None))
                    return
                data.extend(block)
        threads = [threading.Thread(target=drain, args=(proc.stdout, chunks[0]), daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, chunks[1]), daemon=True)]
        for thread in threads: thread.start()
        def feed():
            try:
                proc.stdin.write(stdin);proc.stdin.close()
            except (BrokenPipeError,OSError): pass
        writer=threading.Thread(target=feed, daemon=True);writer.start()
        try:
            code=proc.wait(timeout=min(timeout,max(0,deadline-time.monotonic())))
        except subprocess.TimeoutExpired as exc:
            raise RefinerError('Docker target probe timed out') from exc
        if overflow.is_set(): raise RefinerError('Docker target probe output exceeds limit')
        if time.monotonic() >= deadline:
            raise RefinerError('Validator deadline exhausted before Docker result returned')
    except BaseException as exc:
        error=exc
        cleanup.call('terminate process',lambda _:proc.kill(),
                     max_seconds=cleanup.local_termination_limit(on_cleanup is not None))
    finally:
        # A Docker client can be killed while its Engine container keeps running.
        # Prioritize daemon removal before waiting for local pipes or workers.
        if on_cleanup is not None: on_cleanup(error is not None or code in (125, 126, 127))
        if error is not None:
            cleanup.call('reap process',lambda left:proc.wait(timeout=min(2,left)))
        for index, thread in enumerate(threads + ([writer] if writer is not None else [])):
            if not cleanup.call('join worker '+str(index), lambda left, t=thread:t.join(timeout=left)):
                break
            if thread.is_alive():
                cleanup.incomplete=True;cleanup.reason='cleanup incomplete joining worker '+str(index)
                break
        if not any(t.is_alive() for t in threads + ([writer] if writer is not None else [])):
            for index, stream in enumerate((proc.stdin,proc.stdout,proc.stderr)):
                if stream is not None and not stream.closed:
                    if not cleanup.call('close pipe '+str(index),lambda _, s=stream:s.close()): break
        elif error is None:
            cleanup.incomplete=True;cleanup.reason='cleanup incomplete with active worker'
    if cleanup.incomplete:
        raise RefinerError('cleanup_incomplete: '+str(cleanup.reason)) from error
    if error is not None: raise error
    if time.monotonic() >= deadline:
        raise RefinerError('Validator deadline exhausted before Docker result returned')
    return code, bytes(chunks[0]), bytes(chunks[1])

def execute_python_probe(target, case_id, dependency, contract, *, timeout=None, deadline=None):
    """Execute the exact frozen script with no network or writable host mounts.

    The contract supplies arguments, stdin, expected exit code/stdout, and a
    stdin string that is the actual response. Only deterministic Python CLI
    probes are supported; unsupported contracts are explicitly rejected.
    """
    if not isinstance(case_id, str) or not case_id or dependency.get('asset_kind') != 'script':
        raise RefinerError('Docker probe only supports identified Python script dependencies')
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise RefinerError('Docker probe timeout fallback must be positive when specified')
    if not isinstance(contract, dict) or set(contract) != {'argv', 'stdin', 'expected_exit_code', 'expected_stdout'}:
        raise RefinerError('Docker probe execution contract is incomplete')
    argv = contract['argv']
    if (not isinstance(argv, list) or len(argv) > 32 or any(not isinstance(x, str) or '\x00' in x or len(x) > 4096 for x in argv)
        or type(contract['expected_exit_code']) is not int or not 0 <= contract['expected_exit_code'] <= 124
        or any(not isinstance(contract[x], str) or len(contract[x].encode('utf-8')) > LIMIT for x in ('stdin', 'expected_stdout'))):
        raise RefinerError('Docker probe contract is invalid or exceeds limits')
    started = time.monotonic()
    if deadline is None:
        if timeout is None:
            raise RefinerError('Docker target probe requires a run deadline or an explicit timeout fallback')
        deadline = started + timeout
    elif timeout is not None:
        deadline = min(deadline, started + timeout)
    timeout = deadline - started
    if timeout <= 0:
        raise RefinerError('Validator deadline exhausted before Docker probe')
    rows = inspect_target_dependencies({'planned_target_dependencies': [dependency]}, target)
    row = rows[0]
    if row['status'] != 'ready':
        return {'status': 'not_ready', 'case_id': case_id, 'dependency_id': dependency['id'], 'target': row}
    target = Path(target).resolve()
    path = target.joinpath(*dependency['target_path'].split('/'))
    script = path.read_bytes()
    if _sha(script) != row['sha256']: raise RefinerError('Target dependency changed before Docker probe')
    if len(script) > 1024 * 1024: raise RefinerError('Target dependency exceeds size limit')
    if not dependency['target_path'].endswith('.py'): raise RefinerError('Docker probe requires a Python script')
    docker = shutil.which('docker')
    if not docker: raise RefinerError('Docker is unavailable for target probe')
    container_name = 'refiner-probe-' + secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix='refiner-docker-probe-') as tmp:
        snapshot = Path(tmp) / 'main.py'
        snapshot.write_bytes(script)
        source = str(snapshot.resolve())
        if ',' in source: raise RefinerError('Docker mount source contains an unsupported character')
        command = [docker, 'run', '-i', '--rm', '--name', container_name, '--pull', 'never', '--network', 'none',
            '--read-only', '--tmpfs', '/dev/shm:ro,nosuid,nodev,noexec,size=1m',
            '--tmpfs', '/out:rw,nosuid,nodev,noexec,size=2m', '--workdir', '/out',
            '--user', '65534:65534', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--pids-limit', '32', '--memory', '128m', '--cpus', '1',
            '--mount', 'type=bind,source=' + source + ',target=/asset/main.py,readonly',
            '--entrypoint', '/usr/local/bin/python', IMAGE, '-I', '/asset/main.py', *argv]
        if time.monotonic() >= deadline:
            raise RefinerError('Validator deadline exhausted immediately before Docker launch')
        started = [False]
        cleanup_handled = [False]
        def mark_started(): started[0] = True
        cleanup = _CleanupWindow(deadline)
        error = None
        def container_cleanup(needs_removal):
            if cleanup_handled[0]: return
            cleanup_handled[0] = True
            if not needs_removal: return  # --rm removes a normally exited container
            def remove_container(left):
                result = subprocess.run([docker, 'rm', '-f', container_name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=left, check=False,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result is not None and result.returncode:
                    raise RefinerError('Docker container cleanup failed or could not be verified')
            # Reserve most of the shared grace for daemon-side container removal.
            cleanup.call('remove container', remove_container, force=True, max_seconds=7)
        try:
            code, stdout, stderr = _run_bounded(command, contract['stdin'].encode('utf-8'), timeout,
                deadline=deadline, on_started=mark_started, on_cleanup=container_cleanup, cleanup=cleanup)
        except BaseException as exc:
            error = exc
        finally:
            if started[0] and not cleanup_handled[0]: container_cleanup(True)
        if cleanup.incomplete:
            raise RefinerError('cleanup_incomplete: '+str(cleanup.reason)) from error
        if error is not None: raise error
        if code in (125, 126, 127):
            raise RefinerError('Docker could not start or run the target probe')
        if snapshot.read_bytes() != script or inspect_target_dependencies({'planned_target_dependencies':[dependency]}, target) != rows:
            raise RefinerError('Target or mounted snapshot changed during Docker probe')
    expected = contract['expected_stdout'].encode('utf-8')
    passed = code == contract['expected_exit_code'] and stdout == expected
    evidence = {'schema_version':'skill-refiner.docker-target-probe/v1', 'case_id':case_id,
        'dependency_id':dependency['id'], 'target_root':str(target), 'target_path':dependency['target_path'],
        'target_sha256':row['sha256'], 'image':IMAGE, 'argv':argv,
        'stdin_sha256':_sha(contract['stdin'].encode('utf-8')), 'exit_code':code,
        'stdout_sha256':_sha(stdout), 'stderr_sha256':_sha(stderr),
        'expected_exit_code':contract['expected_exit_code'], 'expected_stdout_sha256':_sha(expected),
        'passed':passed}
    evidence['sha256'] = _sha(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    if time.monotonic() >= deadline:
        raise RefinerError('Validator deadline exhausted before validator result returned')
    return evidence


def validate_case_dependencies(case, target, response, *, deadline=None, on_probe=None, probe_budget=None, phase="evaluation"):
    """Execute frozen validator plus its positive/negative controls.

    The supplied response is the actual upstream response. Unsupported assets
    fail closed; this adapter does not prove the model itself used a tool.
    """
    if not isinstance(response, str):
        raise RefinerError('Actual response is unavailable for validator input')
    dependencies = case.get('planned_target_dependencies', [])
    if dependencies:
        if deadline is None or time.monotonic() >= deadline:
            raise RefinerError('Validator deadline exhausted before reservation')
        if probe_budget is None: raise RefinerError('Persistent validator probe budget is required')
        probe_budget.reserve(phase, 3 * len(dependencies), deadline=deadline)
    records = []
    for dep in dependencies:
        contract = dep.get('execution_contract') or {}
        if dep.get('asset_kind') != 'script' or contract.get('runner') != 'docker-python-response':
            raise RefinerError('Planned dependency execution kind is not supported')
        positive, negative = contract.get('positive_control', {}), contract.get('negative_control', {})
        if (positive.get('input') == negative.get('input') or
            (positive.get('expected_exit_code'), positive.get('expected_stdout')) ==
            (negative.get('expected_exit_code'), negative.get('expected_stdout'))):
            raise RefinerError('Validator controls do not discriminate')
        samples = [('actual', {'input': response, 'expected_exit_code': contract.get('expected_exit_code'),
                              'expected_stdout': contract.get('expected_stdout')}),
                   ('positive', positive), ('negative', negative)]
        for label, sample in samples:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise RefinerError('Wall-clock budget exhausted before validator probe')
            if on_probe is not None: on_probe()
            probe = execute_python_probe(target, case['id'], dep,
                {'argv': contract.get('argv'), 'stdin': sample.get('input'),
                 'expected_exit_code': sample.get('expected_exit_code'),
                 'expected_stdout': sample.get('expected_stdout')}, deadline=deadline)
            if probe.get('status') == 'not_ready':
                raise RefinerError('Planned dependency became unavailable during validator probe')
            records.append({'control': label, 'probe': probe})
    if dependencies and time.monotonic() >= deadline:
        raise RefinerError('Validator deadline exhausted before case result returned')
    return records


def comparable_probe_records(records):
    """Discard path-dependent root and checksum when comparing two real runs."""
    value = json.loads(json.dumps(records, ensure_ascii=False))
    for row in value:
        probe = row['probe']
        recorded = probe.pop('sha256', None)
        expected = _sha(json.dumps(probe, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))
        if recorded != expected:
            raise RefinerError('Validator probe evidence hash does not match')
        row['probe'].pop('target_root', None)
        row['probe'].pop('sha256', None)
    return value
