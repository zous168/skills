"""Offline CLI checks for default and preserved plan-version selection."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from refiner.artifact_validation import all_schema_refs, schema_refs_compatible
from refiner.common import digest, manifest, read_json, write_json, RefinerError
from refiner.pipeline import response_contract_migration
from test_plan_contract import _minimal_plan

class PlanVersionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='refiner-selection-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root/'target'; self.target.mkdir()
        (self.target/'SKILL.md').write_text('---\nname: sample\ndescription: test fixture\n---\n请回复纯文本。\n',encoding='utf8')
        self.stub = self.root/'fake.py'; self.stub.write_text('import sys\nsys.stdin.read()\nprint("{}")\n',encoding='utf8')
        self.exe = Path.home()/'.agents/skills/skill-refiner/scripts/vendor/skill-up/windows-amd64/skill-up.exe'
        if not self.exe.is_file(): self.skipTest('pinned local skill-up unavailable')
        self.common = ['--agent-command',f'{Path(sys.executable).as_posix()} {self.stub.as_posix()}',
                       '--engine','codex','--skill-up',str(self.exe),'--plan-only',
                       '--max-agent-calls','32','--max-seconds','300','--max-cases','2',
                       '--acceptance-cases','2','--planning-attempts','1','--agent-parallelism','1']
    def cli(self, *args):
        return subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/refine.py'),*map(str,args)],
                              cwd=ROOT.parent, capture_output=True, text=True, encoding='utf8',timeout=35)

    def test_new_and_resumed_refine_use_frozen_v7(self):
        run=self.root/'run'
        result=self.cli('run',self.target,*self.common,'--output',run)
        self.assertEqual(result.returncode,2, result.stdout+result.stderr)
        self.assertTrue(read_json(run/'inputs.json')['_response_contract_v7'])
        request=read_json(run/'agent-calls/001-plan/request.json')
        self.assertEqual(request['input']['canonical_response_schema']['properties']['schema_version']['const'], 'skill-refiner.plan/v7')
        self.assertIn('response_contract', request['input']['canonical_response_schema']['properties']['cases']['items']['properties'])
        inputs=read_json(run/'inputs.json')
        result=self.cli('run',self.target,*self.common,'--output',run,'--resume-from',run)
        self.assertEqual(read_json(run/'inputs.json'),inputs)
        self.assertTrue((run/'resume-history/state-001.json').exists(), result.stdout+result.stderr)
        self.assertEqual(read_json(run/'agent-calls/001-plan/request.json')['input']['canonical_response_schema']['properties']['schema_version']['const'],'skill-refiner.plan/v7')

    def test_new_create_freezes_v7_before_analysis_and_resumes_same_input(self):
        run=self.root/'create'
        result=self.cli('create','测试中文咨询技能','--name','demo',*self.common,'--output',run)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        before=read_json(run/'inputs.json'); self.assertTrue(before['_response_contract_v7'])
        result=self.cli('create',*self.common,'--output',run,'--resume-run',run)
        self.assertEqual(read_json(run/'inputs.json'),before)
        self.assertNotIn('input changed',result.stdout.lower()+result.stderr.lower())

    def test_legacy_frozen_plan_reuse_keeps_old_input_shape(self):
        initial=self.root/'initial'
        self.cli('run',self.target,*self.common,'--output',initial)
        context=read_json(initial/'inputs.json'); context.pop('_response_contract_v7')
        previous=self.root/'legacy'; previous.mkdir()
        plan=_minimal_plan(); acceptance=copy.deepcopy(plan)
        acceptance['cases'][0]['prompt'] += ' A fresh independent input.'
        write_json(previous/'inputs.json', context)
        write_json(previous/'test-plan.json',plan);write_json(previous/'acceptance-plan.json',acceptance)
        write_json(previous/'oracle.lock.json',{'source_manifest':manifest(self.target),
                   'sha256':digest({'plan':plan,'acceptance':acceptance,'inputs':context})})
        result=self.cli('run',self.target,*self.common,'--output',self.root/'reused','--reuse-plan',previous)
        reused=read_json(self.root/'reused/inputs.json')
        self.assertEqual(reused,context)
        self.assertNotIn('Cached plans refer to different Skill inputs',result.stdout+result.stderr)
        self.assertIn('"plan_generation", "status": "reused"',result.stdout)

    def test_schema_registry_allows_only_known_additions(self):
        current=all_schema_refs()
        added={'skill-refiner.plan-v7','skill-refiner.plan-review-recovery-v6',
               'skill-refiner.response-applicability-analysis-v1','skill-refiner.response-applicability-decision-v1',
               'skill-refiner.response-contract-migration-v1'}
        old=[entry for entry in current if entry['id'] not in added]
        self.assertTrue(schema_refs_compatible(old,current))
        for optional in added:
            self.assertTrue(schema_refs_compatible([entry for entry in current if entry['id'] != optional],current))
        broken=copy.deepcopy(old);broken[0]['sha256']='0'*64
        self.assertFalse(schema_refs_compatible(broken,current))
        self.assertFalse(schema_refs_compatible(old[:-1],current))
        self.assertFalse(schema_refs_compatible(old+[old[0]],current))

    def test_recovery_migration_evidence_is_reproducible_and_immutable(self):
        inputs={'requirements':[],'files':[]}
        recovery={'previous_plan':{'schema_version':'skill-refiner.plan/v5'},
                  'usage':{'agent_calls':2,'elapsed_seconds':10.0,'target_evaluations':0}}
        contract={'refine_plan_recovery_sha256':'a'*64}
        record=response_contract_migration(self.root,inputs,recovery,contract,create=True)
        self.assertEqual(record['selected_plan_schema_version'],'skill-refiner.plan/v7')
        self.assertEqual(response_contract_migration(self.root,inputs,recovery,contract),record)
        changed=copy.deepcopy(recovery);changed['usage']['agent_calls']=0
        with self.assertRaises(RefinerError): response_contract_migration(self.root,inputs,changed,contract)
        with self.assertRaises(RefinerError): response_contract_migration(self.root,inputs,recovery,{})

if __name__ == '__main__': unittest.main()
