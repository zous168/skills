from pathlib import Path
import re
import unittest


STANDARD = (Path(__file__).resolve().parents[1] / "references" /
            "skill-stage-artifact-standard.md").read_text(encoding="utf-8")
STANDARD_ONE_LINE = re.sub(r"\s+", " ", STANDARD)


class StageArtifactStandardScopeTests(unittest.TestCase):
    def test_standard_separates_controller_target_and_runner_ownership(self):
        for owner in ("**Controller:**", "**Target runtime:**", "**Execution runner:**"):
            with self.subTest(owner=owner):
                self.assertIn(owner, STANDARD)

    def test_response_schema_does_not_imply_duplicate_runtime_validator(self):
        self.assertIn("Package a schema for a structured output", STANDARD)
        self.assertIn("testing a response alone does not require a second validator implementation inside the target Skill", STANDARD_ONE_LINE)

    def test_ephemeral_self_review_preserves_public_contract(self):
        self.assertIn("Keep its record ephemeral", STANDARD_ONE_LINE)
        self.assertIn("never add the private review record to that response", STANDARD_ONE_LINE)
        self.assertIn("Do not add target recovery APIs", STANDARD_ONE_LINE)

    def test_future_target_assets_are_plannable_but_not_claimed_as_passed(self):
        self.assertIn("Mark them as planned dependencies", STANDARD_ONE_LINE)
        self.assertIn("do not require those future files to exist at the plan gate", STANDARD_ONE_LINE)
        self.assertIn("do not count them as implemented or passed", STANDARD_ONE_LINE)

    def test_each_applicable_criterion_stays_traceable_without_duplicate_cases(self):
        self.assertIn("every applicable atomic criterion must map", STANDARD_ONE_LINE)
        self.assertIn("a single test may cover multiple criteria", STANDARD_ONE_LINE)
        self.assertIn("Do not exceed a frozen case budget", STANDARD_ONE_LINE)


if __name__ == "__main__":
    unittest.main()
