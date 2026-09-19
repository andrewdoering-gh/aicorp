import asyncio
import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "aicorp_approval_workflow.py"
SPEC = importlib.util.spec_from_file_location("aicorp_approval_workflow", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ApprovalWorkflowResetTests(unittest.TestCase):
    def setUp(self):
        self.tool = MODULE.Tools()
        self.responses = []
        self.calls = []

        def request(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            return self.responses.pop(0)

        self.tool._request = request

    def test_list_pending_approvals_returns_terminal_listing_without_follow_up_call(self):
        self.responses = [
            {
                "requests": [
                    {"id": 101, "action": "read_health_report", "status": "pending"},
                    {"id": 102, "action": "deploy", "status": "approved"},
                ]
            }
        ]

        result = asyncio.run(self.tool.list_pending_approvals())

        self.assertIn("FINAL RESPONSE REQUIRED", result)
        self.assertIn("Stop tool use now", result)
        self.assertIn("Approval 101", result)
        self.assertIn("Action: read_health_report", result)
        self.assertNotIn("Approval 102", result)
        self.assertNotIn("{", result)
        self.assertEqual(self.calls, [("/approval-requests", "GET", None)])

    def test_list_pending_approvals_formats_nested_context_as_readable_fields(self):
        self.responses = [
            {
                "requests": [
                    {
                        "id": 109,
                        "action": "generate_technical_plan",
                        "status": "pending",
                        "requested_by": "andrew",
                        "context": {
                            "revision_type": "amendment",
                            "dependencies": ["aicorp-apps/agent/acceptance.py"],
                        },
                    }
                ]
            }
        ]

        result = asyncio.run(self.tool.list_pending_approvals())

        self.assertIn("Approval 109", result)
        self.assertIn("Revision type: amendment", result)
        self.assertIn("Dependencies:", result)
        self.assertIn("- aicorp-apps/agent/acceptance.py", result)
        self.assertNotIn('"revision_type"', result)

    def test_reset_creates_pending_request_with_preview_scope(self):
        self.responses = [
            {
                "reset": {"planning_generation": 1, "product_brief_ids": [4]},
                "confirmation_phrase": "RESET_PLANNING_WORKSPACE",
            },
            {"id": 18, "status": "pending", "action": "reset_planning_workspace"},
        ]

        result = json.loads(asyncio.run(self.tool.reset_planning_workspace()))

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["reset"]["product_brief_ids"], [4])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0], "/planning-reset/preview")
        self.assertEqual(self.calls[1][0], "/planning-reset/request")
        self.assertEqual(
            self.calls[1][2],
            {
                "reason": "Previous Product Brief goals were not realized; start a new governed planning generation.",
                "confirm": True,
            },
        )

    def test_approve_request_posts_with_only_the_request_id(self):
        self.responses = [{"request": {"id": 18, "status": "approved"}}]

        result = json.loads(
            asyncio.run(
                self.tool.approve_request(18, "Reviewed the request.")
            )
        )

        self.assertEqual(result["request"]["status"], "approved")
        self.assertEqual(
            self.calls,
            [
                (
                    "/approval-requests/18",
                    "POST",
                    {
                        "status": "approved",
                        "decision_reason": "Reviewed the request.",
                    },
                )
            ],
        )

    def test_deny_request_posts_with_only_the_request_id(self):
        self.responses = [{"request": {"id": 18, "status": "denied"}}]

        result = json.loads(asyncio.run(self.tool.deny_request(18)))

        self.assertEqual(result["request"]["status"], "denied")
        self.assertEqual(self.calls[0][0], "/approval-requests/18")
        self.assertEqual(self.calls[0][2]["status"], "denied")

    def test_legacy_reset_approval_forwards_to_generic_approval(self):
        self.responses = [{"request": {"id": 18, "status": "approved"}}]

        result = json.loads(
            asyncio.run(
                self.tool.approve_planning_reset(
                    18,
                    decision_reason="Reviewed the reset scope.",
                )
            )
        )

        self.assertEqual(result["request"]["status"], "approved")
        self.assertEqual(self.calls[0][0], "/approval-requests/18")
        self.assertEqual(
            self.calls[0][2],
            {
                "status": "approved",
                "decision_reason": "Reviewed the reset scope.",
            },
        )


if __name__ == "__main__":
    unittest.main()