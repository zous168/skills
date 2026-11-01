"""Persistent conservative reservations for v8 validator executions."""
from __future__ import annotations
import json
import math
import os
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from .common import RefinerError, digest

class ProbeBudget:
    def __init__(self, root, config, deadline):
        self.directory = Path(root) / 'validator-probe-budget'
        self.deadline = deadline
        self.directory.mkdir(exist_ok=True)
        info = self.directory.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or not self.directory.is_dir():
            raise RefinerError('Unsafe validator budget directory')
        self.config = config
        self._lock()
        try:
            path = self.directory / 'config.json'
            if not path.exists():
                if any(self.directory.glob('reservation-*.json')):
                    raise RefinerError('Validator budget configuration is missing despite reservations')
                with path.open('x', encoding='utf-8') as stream:
                    json.dump(config, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
                with (self.directory / 'highwater.json').open('x', encoding='utf-8') as stream:
                    json.dump({'evaluation': 0, 'final': 0, 'sessions': 0, 'elapsed_seconds': 0.0}, stream); stream.flush(); os.fsync(stream.fileno())
            elif self._read(path) != config:
                raise RefinerError('Validator budget differs from frozen plan or limits')
            self._counts()
        finally: self._unlock()
    @staticmethod
    def configuration(plans, executor_count, max_rounds):
        if type(executor_count) is not int or executor_count < 1 or type(max_rounds) is not int or max_rounds < 0:
            raise RefinerError('Invalid validator budget inputs')
        development, acceptance = plans
        d=sum(len(c.get('planned_target_dependencies', [])) for c in development['cases'])
        a=sum(len(c.get('planned_target_dependencies', [])) for c in acceptance['cases'])
        factor=3*executor_count
        return {'schema_version':'skill-refiner.validator-probe-budget/v1',
                'plans_sha256':digest({'development':development, 'acceptance':acceptance}),
                'executor_count':executor_count, 'max_rounds':max_rounds,
                'evaluation_limit':factor*(d+a)*(max_rounds+1),
                'final_limit':2*factor*(d+a), 'max_final_passes':2}
    def _lock(self):
        try: (self.directory / '.lock').mkdir()
        except FileExistsError as exc:
            raise RefinerError('Validator budget is busy or has an unresolved interrupted reservation') from exc
    def _unlock(self): (self.directory / '.lock').rmdir()
    def _read(self, path):
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or not stat.S_ISREG(info.st_mode):
            raise RefinerError('Unsafe validator budget record')
        try: return json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc: raise RefinerError('Invalid validator budget record') from exc
    def _counts(self):
        totals={'evaluation':0,'final':0}
        for path in self.directory.glob('reservation-*.json'):
            row=self._read(path)
            if (not isinstance(row, dict) or set(row) != {'stage','count','created_at'} or
                row['stage'] not in totals or type(row['count']) is not int or row['count'] < 1 or
                not isinstance(row['created_at'],float)):
                raise RefinerError('Invalid validator reservation')
            totals[row['stage']]+=row['count']
        floor_path=self.directory / 'highwater.json'
        if not floor_path.is_file(): raise RefinerError('Validator budget high-water mark is missing')
        floor=self._read(floor_path)
        if (not isinstance(floor,dict) or set(floor) != {'evaluation','final','sessions','elapsed_seconds'} or
                any(type(floor[x]) is not int or floor[x] < 0 or totals[x] < floor[x] for x in totals) or
                type(floor['sessions']) is not int or floor['sessions'] < 0 or
                type(floor['elapsed_seconds']) not in (int,float) or not math.isfinite(floor['elapsed_seconds']) or floor['elapsed_seconds'] < 0):
            raise RefinerError('Validator budget records were lost or rolled back')
        sessions = [self._read(path) for path in self.directory.glob('verify-session-*.json')]
        sequences = [row.get('sequence') if isinstance(row,dict) else None for row in sessions]
        if (len(sequences) < floor['sessions'] or any(type(x) is not int or x < 1 for x in sequences)
                or sorted(sequences) != list(range(1,len(sequences)+1))):
            raise RefinerError('Validator verification sessions were lost or rolled back')
        if any(totals[x] > self.config[x+'_limit'] for x in totals):
            raise RefinerError('Validator reservations exceed frozen budget')
        return totals
    def reserve(self, stage, count, *, deadline=None):
        if stage not in ('evaluation','final') or type(count) is not int or count < 1:
            raise RefinerError('Invalid validator reservation request')
        self._lock()
        try:
            limits=[d for d in (self.deadline,deadline) if d is not None]
            if not limits or time.monotonic() >= min(limits):
                raise RefinerError('Validator deadline exhausted before reservation')
            totals=self._counts()
            if totals[stage] + count > self.config[stage+'_limit']:
                raise RefinerError('Validator '+stage+' probe budget exhausted: requires '+str(count)+
                                   ', remaining '+str(self.config[stage+'_limit']-totals[stage]))
            path=self.directory / ('reservation-'+secrets.token_hex(16)+'.json')
            totals[stage] += count
            floor = self._read(self.directory / 'highwater.json')
            floor.update(totals)
            self._write_floor(floor)
            with path.open('x', encoding='utf-8') as stream:
                json.dump({'stage':stage,'count':count,'created_at':float(time.time())},stream)
                stream.flush();os.fsync(stream.fileno())
            return totals[stage]
        finally: self._unlock()


    def _write_floor(self, value):
        temp = self.directory / ('highwater-'+secrets.token_hex(8)+'.tmp')
        with temp.open('x',encoding='utf-8') as stream:
            json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,self.directory / 'highwater.json')

    def _verified_sessions(self):
        floor=self._read(self.directory / 'highwater.json')
        sessions=[]
        for path in self.directory.glob('verify-session-*.json'):
            row=self._read(path)
            if not isinstance(row,dict) or type(row.get('sequence')) is not int or row['sequence'] < 1:
                raise RefinerError('Invalid validator verification session')
            sessions.append(row)
        sessions.sort(key=lambda x:x['sequence'])
        if len(sessions) < floor['sessions'] or [x['sequence'] for x in sessions] != list(range(1,len(sessions)+1)):
            raise RefinerError('Validator verification sessions were lost or rolled back')
        elapsed=0.0
        for row in sessions:
            if row.get('status') != 'finished':
                raise RefinerError('Unresolved validator verification session requires reconciliation')
            before, after, seconds = row.get('elapsed_before'), row.get('elapsed_after'), row.get('seconds')
            if (any(type(x) not in (int,float) or not math.isfinite(x) for x in (before,after,seconds)) or
                    before < elapsed or seconds < 0 or after < before or abs((after-before)-seconds)>1e-7):
                raise RefinerError('Invalid validator verification duration')
            elapsed=after
        if elapsed < floor['elapsed_seconds']:
            raise RefinerError('Validator verification time was lost or rolled back')
        return sessions, elapsed

    @contextmanager
    def final_session(self, max_seconds, elapsed_seconds):
        if (type(max_seconds) not in (int, float) or type(elapsed_seconds) not in (int, float)
                or not math.isfinite(max_seconds) or not math.isfinite(elapsed_seconds)
                or not 0 <= elapsed_seconds <= max_seconds):
            raise RefinerError('Invalid validator elapsed-time budget')
        self._lock()
        try:
            self._counts()
            sessions, session_elapsed=self._verified_sessions()
            elapsed_before=max(float(elapsed_seconds),session_elapsed)
            remaining=max_seconds-elapsed_before
            now=time.monotonic()
            if self.deadline is not None: remaining=min(remaining, self.deadline-now)
            if remaining <= 0: raise RefinerError('Validator deadline exhausted before final verification')
            sequence=len(sessions)+1
            path=self.directory / ('verify-session-'+secrets.token_hex(16)+'.json')
            floor=self._read(self.directory/'highwater.json')
            floor['sessions']=sequence
            self._write_floor(floor)
            with path.open('x',encoding='utf-8') as stream:
                json.dump({'status':'running','sequence':sequence,'elapsed_before':elapsed_before},stream)
                stream.flush();os.fsync(stream.fileno())
        finally: self._unlock()
        begun=time.monotonic()
        try:
            yield begun+remaining
        finally:
            duration=time.monotonic()-begun
            elapsed_after=elapsed_before+duration
            self._lock()
            try:
                with path.open('w',encoding='utf-8') as stream:
                    json.dump({'status':'finished','sequence':sequence,'elapsed_before':elapsed_before,
                               'elapsed_after':elapsed_after,'seconds':duration},stream)
                    stream.flush();os.fsync(stream.fileno())
                floor=self._read(self.directory/'highwater.json')
                floor['elapsed_seconds']=max(floor['elapsed_seconds'],elapsed_after)
                self._write_floor(floor)
            finally: self._unlock()
