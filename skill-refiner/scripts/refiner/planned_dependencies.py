"""Planning and runtime validation for v8 future target dependencies.

Planning validates contracts and provenance, not file availability or behavior.
Runtime presence is an additional precondition, never evidence that assertions pass.
"""
from __future__ import annotations
import hashlib
import re
import stat
from pathlib import Path, PureWindowsPath
from .common import RefinerError

_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)


def _safe_relative(value):
    if not isinstance(value, str) or not value or len(value) > 240 or "\\" in value:
        raise RefinerError("Planned target path must be a nonempty portable slash-separated relative path")
    p=PureWindowsPath(value)
    parts=value.split('/')
    if (p.is_absolute() or p.drive or any(not part or part in {'.','..'} or ':' in part or
            part.rstrip(' .') != part or _RESERVED.fullmatch(part) or any(ord(c)<32 for c in part)
            for part in parts) or parts[0].lower() in {'.git','.codex','.agents','.cursor','evals','tests'}):
        raise RefinerError("Planned target path is unsafe or protected")
    return parts


def validate_planned_dependencies(plan, verified_sources):
    """Check exact links against authenticated inputs and frozen plan elements."""
    if not isinstance(verified_sources, dict):
        raise RefinerError("Planned dependencies require authenticated source hashes")
    for key, value in verified_sources.items():
        if not isinstance(key,str) or not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value):
            raise RefinerError("Planned dependency source bindings are invalid")
    requirements={row['id'] for row in plan['requirements']}
    steps={row['id']:row for row in plan['execution_plan']['steps']}
    declared_schemas=plan['stage_contracts']['schemas']
    for case in plan['cases']:
        ids=set()
        assertions={row['id'] for row in case['checks']}
        case_requirements=set(case['requirement_ids'])
        for item in case['planned_target_dependencies']:
            if item['id'] in ids:
                raise RefinerError("Duplicate planned dependency ID in case: " + case['id'])
            ids.add(item['id']); _safe_relative(item['target_path'])
            reqs=set(item['requirement_ids'])
            if not reqs <= requirements or not reqs <= case_requirements:
                raise RefinerError("Planned dependency requirement IDs do not match the case")
            if not set(item['source_refs']) <= verified_sources.keys():
                raise RefinerError("Planned dependency cites an unauthenticated source")
            step=steps.get(item['implementation_step_id'])
            if not step or not reqs <= set(step['requirement_ids']) or case['id'] not in step['case_ids']:
                raise RefinerError("Planned dependency is not linked to an implementation step and case")
            for scenario in ('positive_scenario','negative_scenario'):
                row=item[scenario]
                if not set(row['assertion_ids']) <= assertions:
                    raise RefinerError("Planned dependency scenario cites unknown assertion IDs")
                if not row['stimulus'].strip() or not row['expected'].strip():
                    raise RefinerError("Planned dependency scenario requires concrete behavior")
            if item['positive_scenario']==item['negative_scenario']:
                raise RefinerError("Planned dependency positive and negative scenarios must differ")
            contract = item.get('execution_contract')
            if not isinstance(contract, dict):
                raise RefinerError('Planned dependency execution contract is missing')
            positive = contract.get('positive_control', {})
            negative = contract.get('negative_control', {})
            if (positive.get('input') == negative.get('input') or
                    (positive.get('expected_exit_code'), positive.get('expected_stdout')) ==
                    (negative.get('expected_exit_code'), negative.get('expected_stdout'))):
                raise RefinerError('Validator positive and negative controls must discriminate')
            if (contract.get('expected_exit_code'), contract.get('expected_stdout')) != (positive.get('expected_exit_code'), positive.get('expected_stdout')):
                raise RefinerError('Actual validator expectation must match the positive control')
            if item['asset_kind']=='schema' and item['target_path'] not in {x['path'] for x in declared_schemas}:
                raise RefinerError("Planned schema is not declared in the stage contract")
            if (item['asset_kind'] != 'script' or not item['target_path'].endswith('.py')
                    or contract.get('runner') != 'docker-python-response'):
                raise RefinerError('Planned dependency execution is unsupported: only single-file .py scripts with docker-python-response are supported')
    return True


def inspect_target_dependencies(case, target):
    """Inspect actual target files without following links; return explicit status.

    A ready result only proves files are present and hashable, never execution
    or correctness. The caller must still run all frozen assertions.
    """
    root=Path(target)
    try:
        info=root.lstat()
    except OSError as exc:
        raise RefinerError("Cannot inspect target root for planned dependencies") from exc
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or not stat.S_ISDIR(info.st_mode):
        raise RefinerError("Planned dependency target root is not a safe directory")
    result=[]
    for item in case.get('planned_target_dependencies',[]):
        parts=_safe_relative(item['target_path']);path=root;missing=False
        for component in parts:
            path=path/component
            try: info=path.lstat()
            except FileNotFoundError:
                missing=True;break
            except OSError as exc:
                raise RefinerError("Cannot inspect planned target dependency: "+item['target_path']) from exc
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                raise RefinerError("Planned target dependency uses a linked path")
        if missing:
            result.append({'id':item['id'],'path':item['target_path'],'status':'not_ready','sha256':None})
        else:
            if not stat.S_ISREG(info.st_mode):
                raise RefinerError("Planned target dependency is not a regular file: "+item['target_path'])
            try: sha=hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:raise RefinerError("Cannot read planned target dependency: "+item['target_path']) from exc
            result.append({'id':item['id'],'path':item['target_path'],'status':'ready','sha256':sha})
    return result
