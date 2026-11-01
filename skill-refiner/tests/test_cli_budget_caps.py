import contextlib
import io
from unittest import TestCase, mock

from refiner import cli


class WorkflowBudgetCapTests(TestCase):
    def test_approved_7200_second_workflow_cap_is_accepted(self):
        fake_state = {"status": "planned", "workspace": "D:/tmp/refiner-budget-test"}
        with mock.patch("refiner.creation.run", return_value=fake_state) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            result = cli.main([
                "create", "demo", "--config", "unused.json", "--output", "D:/tmp/refiner-budget-test",
                "--max-seconds", "7200", "--max-agent-calls", "84", "--max-evaluations", "80",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.args[0].max_seconds, 7200)
        self.assertEqual(run.call_args.args[0].max_agent_calls, 84)

    def test_workflow_cap_above_7200_seconds_is_rejected_before_run(self):
        with mock.patch("refiner.creation.run") as run, contextlib.redirect_stderr(io.StringIO()):
            result = cli.main([
                "create", "demo", "--config", "unused.json", "--output", "D:/tmp/refiner-budget-test",
                "--max-seconds", "7201",
            ])
        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_compare_remains_limited_to_3600_seconds(self):
        with contextlib.redirect_stderr(io.StringIO()):
            result = cli.main([
                "compare", "D:/tmp/skill", "--suite", "D:/tmp/suite", "--config", "unused.json",
                "--max-seconds", "7200",
            ])
        self.assertEqual(result, 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
