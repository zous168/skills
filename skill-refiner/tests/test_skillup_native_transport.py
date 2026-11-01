"""Real pinned skill-up SessionResult transport, with a local stub and no model."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from refiner.native_tools import native_tool_events
from refiner.runtime import command_exit_record


class SkillupTransportTests(unittest.TestCase):
    def test_real_transport_preserves_collector_exit_codes_and_rejects_tampering(self):
        configured = os.environ.get("SKILL_REFINER_TEST_SKILL_UP")
        executable = Path(configured) if configured else (
            Path.home() / ".agents/skills/skill-refiner/scripts/vendor/skill-up/windows-amd64/skill-up.exe")
        if not executable.is_file():
            self.skipTest("Pinned local skill-up executable is not installed; no download/model fallback")
        if os.name == "nt" and not Path("C:/Program Files/Git/bin/bash.exe").is_file():
            self.skipTest("Pinned script-judge transport requires local Git Bash")
        with tempfile.TemporaryDirectory(prefix="refiner-transport-") as temporary:
            root = Path(temporary)
            target = root / "fixture-skill"
            (target / "evals").mkdir(parents=True)
            (target / "SKILL.md").write_text(
                "---\nname: offline-exit-transport\ndescription: 本地协议测试，无模型调用。\n---\n返回测试结果。",
                encoding="utf-8")
            for identifier, code in (("zero", 0), ("nonzero", 7), ("tampered", 0)):
                message = "observed-" + str(code)
                trace, _, _ = native_tool_events([{
                    "type": "event_msg", "payload": {"type": "item_completed", "item": {
                        "type": "CommandExecution", "id": "command", "command": "fixture-command",
                        "exit_code": code, "aggregated_output": message}}}], "fixture")
                record = command_exit_record(trace)
                if identifier == "tampered":
                    record = copy.deepcopy(record)
                    record["results"]["fixture:command"]["content_sha256"] = "0" * 64
                trace.append({"role": "system", "content": json.dumps(
                    {"skill_refiner_command_exit_evidence": record})})
                session = {"exit_code": 0, "final_message": message, "transcript": trace,
                           "artifacts": {"files": []}}
                (root / (identifier + ".session.json")).write_text(json.dumps(session), encoding="utf-8")
            stub = root / "stub.py"
            stub.write_text(
                "import pathlib,sys\n"
                "request=pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
                "name='tampered' if 'fixture-tampered' in request else 'nonzero' if 'fixture-nonzero' in request else 'zero'\n"
                f"source=pathlib.Path({str(root)!r})/(name+'.session.json')\n"
                "output=pathlib.Path(sys.argv[2]);output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(source.read_bytes())\n",
                encoding="utf-8")
            judge = root / "judge.py"
            judge.write_text(
                "import json,os,pathlib,sys\n"
                f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                "from refiner.checks import check_one\n"
                "name,code=sys.argv[1],int(sys.argv[2])\n"
                "trace=json.loads(pathlib.Path(os.environ['EVAL_TRANSCRIPT_PATH']).read_text(encoding='utf-8'))\n"
                "response=os.environ['EVAL_FINAL_MESSAGE']\n"
                "passed,diagnostic=check_one({'type':'tool_output_matches','value':'fixture-command','expected_exit_code':code},response,'.',trace)\n"
                "absence,absence_diag=check_one({'type':'tool_call_absent','value':'web|http|network'},response,'.',trace)\n"
                "expected=name!='tampered'\n"
                "ok=passed==expected and not absence\n"
                "audit={'passed':passed,'expected':expected,'absence':absence,'diagnostic':diagnostic,"
                "'exit_field_preserved':any('exit_code' in r.get('tool_result',{}) for r in trace),"
                "'system_evidence_preserved':any('skill_refiner_command_exit_evidence' in r.get('content','') for r in trace)}\n"
                f"pathlib.Path({str(root)!r},name+'.audit.json').write_text(json.dumps(audit),encoding='utf-8')\n"
                "print(json.dumps({'case_id':name,'passed':ok,'assertions':[]}))\n"
                "sys.exit(0 if ok else 1)\n", encoding="utf-8")
            case_paths = []
            for identifier, code in (("zero", 0), ("nonzero", 7), ("tampered", 0)):
                shell = root / (identifier + ".judge.sh")
                shell.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(value) for value in
                    (sys.executable, "-B", "-X", "utf8", str(judge), identifier, str(code))) + "\n", encoding="utf-8")
                case = {"id": identifier, "title": identifier, "input": {"prompt": "fixture-" + identifier},
                        "context": {"files": {}}, "constraints": {"timeout_seconds": None},
                        "judge": {"type": "script", "script_path": str(shell), "timeout_seconds": None}}
                relative = "evals/" + identifier + ".yaml"
                (target / relative).write_text(json.dumps(case), encoding="utf-8")
                case_paths.append(relative)
            input_file, output_file = "$" + "{input_file}", "$" + "{output_file}"
            engine = {"name": "offline-local-session", "custom": {
                "transport": "local", "response_format": "session_result", "local": {
                    "command": sys.executable, "args": [str(stub), input_file, output_file],
                    "input_file": input_file, "output_file": output_file}}}
            config = {"schema_version": "v1alpha1", "environment": {"type": "none"},
                      "skills": [{"source": "local_path", "path": "."}], "engine": engine,
                      "cases": {"files": case_paths, "parallelism": 1, "defaults": {"timeout_seconds": None}},
                      "benchmark": {"enabled": False}, "report": {"formats": ["json"]}}
            configuration = target / "evals/config.yaml"
            configuration.write_text(json.dumps(config), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            if os.name == "nt":
                env["PATH"] = "C:/Program Files/Git/bin" + os.pathsep + env.get("PATH", "")
            for arguments in ([str(executable), "validate", str(configuration)],
                              [str(executable), "run", str(configuration), "--output-dir", str(root / "upstream")]):
                result = subprocess.run(arguments, cwd=target, env=env, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            audits = {name: json.loads((root / (name + ".audit.json")).read_text(encoding="utf-8"))
                      for name in ("zero", "nonzero", "tampered")}
            self.assertTrue(audits["zero"]["passed"])
            self.assertTrue(audits["nonzero"]["passed"])
            self.assertFalse(audits["tampered"]["passed"])
            self.assertTrue(all(not audit["exit_field_preserved"] for audit in audits.values()),
                            "This pinned transport control must exercise the stripped-field path")
            self.assertTrue(all(audit["system_evidence_preserved"] for audit in audits.values()))
            self.assertTrue(all(not audit["absence"] for audit in audits.values()))


if __name__ == "__main__":
    unittest.main()
