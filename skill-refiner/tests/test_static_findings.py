import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from refiner.artifact_validation import content_schema_id, schema_ref, validate_artifact


class StaticFindingsArtifactTest(unittest.TestCase):
    def test_controller_findings_accept_empty_and_populated_arrays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "drafts" / "1" / "static-findings.json"
            path.parent.mkdir(parents=True)
            for value in ([], [{"id": "S-001", "rule": "format.entrypoint", "path": "SKILL.md",
                                "message": "Missing SKILL.md", "severity": "error"}]):
                data = json.dumps(value).encode("utf-8")
                path.write_bytes(data)
                ref = schema_ref("skill-refiner.static-findings")
                record = {"path": "drafts/1/static-findings.json", "media_type": "application/json",
                          "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "schema_ref": ref, "validation": {"status": "pending", "validator_id": "json-schema"}}
                validate_artifact(path, root, record)
                self.assertEqual(record["validation"]["status"], "passed")

    def test_target_file_is_not_mistaken_for_controller_findings(self):
        self.assertEqual(content_schema_id(Path("drafts/1/skill/static-findings.json")),
                         "skill-refiner.json-object")

    def test_controller_rejects_invalid_finding(self):
        from refiner.artifact_validation import validate_value
        with self.assertRaises(ValueError):
            validate_value([{"severity": "error"}], schema_ref("skill-refiner.static-findings"))


if __name__ == "__main__":
    unittest.main()
