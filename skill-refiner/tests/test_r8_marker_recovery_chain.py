"""Offline second-generation migration of the real r7 plan via temporary controllers."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_r7_amended_recovery import SOURCE, ARCHIVE, SNAPSHOT, ANCESTOR
from refiner.common import read_json, write_json
from refiner.refine_plan_recovery import _chain_tree, _io_path, _safe_file, migrate, verify_recovery


@unittest.skipUnless(all(p.exists() for p in (SOURCE, ARCHIVE, SNAPSHOT, ANCESTOR)), 'terminal r7 unavailable')
class MarkerSuccessorRecoveryTests(unittest.TestCase):
    def test_unexecuted_marker_migration_is_authenticated_again_next_generation(self):
        root = Path(tempfile.mkdtemp(prefix='r8-marker-chain-'))
        source_before = _chain_tree(SOURCE)
        try:
            package = Path(__file__).resolve().parents[1]
            archive = root / 'controller'
            for relative in _chain_tree(package, archive=True):
                dest = _io_path(archive / relative, force=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_safe_file(package, relative), dest)
            # Distinct controller identity, no functional change; this copy is the r8 controller archive.
            recovery = archive / 'scripts/refiner/refine_plan_recovery.py'
            with recovery.open('a', encoding='utf-8') as f:
                f.write('\n# isolated offline successor-controller fixture\n')
            r8, r9 = root / 'r8', root / 'r9'
            code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                    'from refiner.refine_plan_recovery import migrate;'
                    'migrate(sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5],'
                    'source_snapshot=sys.argv[6],planned_dependency_migration=True)')
            child = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', code, str(archive / 'scripts'),
                                    str(SOURCE), str(ARCHIVE), str(ANCESTOR), str(r8), str(SNAPSHOT)],
                                   capture_output=True, text=True, encoding='utf-8', timeout=360)
            self.assertEqual(child.returncode, 0, child.stderr)
            r8_before = _chain_tree(r8)
            rows = [{'path': name, 'sha256': sha, 'bytes': _safe_file(r8, name).stat().st_size}
                    for name, sha in r8_before.items()]
            snapshot = root / 'r8-snapshot.json'
            write_json(snapshot, {'source': str(r8), 'files': rows})
            migrate(r8, archive, ANCESTOR, r9, source_snapshot=snapshot)
            contract = read_json(r9 / 'resume-contract.json')
            result = verify_recovery(r9, contract)
            self.assertEqual(result['usage'], {'agent_calls': 27, 'elapsed_seconds': 2453.82, 'target_evaluations': 0})
            self.assertEqual(len(result['subsequent_controls']), 6)
            self.assertEqual(result['pending_prelaunch_call'], '013-analysis-review')
            self.assertEqual(result['latest_control_plan'], read_json(SOURCE / 'agent-calls/025-plan/response.json'))
            self.assertEqual(result['planned_dependency_migration'], read_json(r8 / 'planned-dependency-migration.json'))
            self.assertEqual((r9 / 'refine-budget-amendment.json').read_bytes(),
                             (r8 / 'refine-budget-amendment.json').read_bytes())
            self.assertEqual((contract['options']['max_cases'], contract['options']['acceptance_cases'],
                              contract['limits']['max_evaluations']), (24, 12, 120))
            self.assertEqual(_chain_tree(r8), r8_before)
            self.assertEqual(_chain_tree(SOURCE), source_before)
        finally:
            shutil.rmtree(_io_path(root, force=True))


if __name__ == '__main__':
    unittest.main()
