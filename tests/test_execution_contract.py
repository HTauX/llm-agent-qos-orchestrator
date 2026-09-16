import json
import unittest

from qos_system_lg.execution_contract import parse_executor_request, validate_executor_request


class TestExecutionContract(unittest.TestCase):
    def test_parses_nested_request(self) -> None:
        request = {
            "capability": "execute_change",
            "params": {
                "plan": {"plan_id": "PLAN-001", "plan_ok": True},
                "cli_config": "qos flow rebalance\ncommit",
            },
        }

        capability, plan, cli_config = parse_executor_request(json.dumps(request))

        self.assertEqual(capability, "execute_change")
        self.assertEqual(plan["plan_id"], "PLAN-001")
        self.assertIn("commit", cli_config)

    def test_rejects_plan_marked_invalid(self) -> None:
        error = validate_executor_request(
            capability="execute_change",
            plan={"plan_id": "PLAN-001", "plan_ok": False},
            cli_config="commit",
        )

        self.assertEqual(error, "plan_ok must be explicitly true")

    def test_rejects_plan_without_explicit_approval(self) -> None:
        error = validate_executor_request(
            capability="execute_change",
            plan={"plan_id": "PLAN-001"},
            cli_config="qos flow rebalance\ncommit",
        )

        self.assertEqual(error, "plan_ok must be explicitly true")

    def test_rejects_missing_cli_config(self) -> None:
        error = validate_executor_request(
            capability="execute_change",
            plan={"plan_id": "PLAN-001", "plan_ok": True},
            cli_config="",
        )

        self.assertEqual(error, "missing cli_config")


if __name__ == "__main__":
    unittest.main()
