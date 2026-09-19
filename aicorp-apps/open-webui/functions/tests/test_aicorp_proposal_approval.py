import asyncio
import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "aicorp_proposal_approval.py"
SPEC = importlib.util.spec_from_file_location("aicorp_proposal_approval", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ProposalApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tool = MODULE.Tools()
        self.responses = []
        self.calls = []

        def request(path, method="GET", payload=None):
            self.calls.append((path, method, payload))
            return self.responses.pop(0)

        self.tool._request = request

    def test_retry_request_creates_an_automatically_approved_request(self):
        self.responses = [
            {
                "id": 19,
                "status": "approved",
                "action": "retry_deployment",
                "source_deployment_approval_id": 18,
            }
        ]

        result = json.loads(
            asyncio.run(
                self.tool.request_deployment_retry(
                    18,
                    "Retry after correcting the deployment failure.",
                )
            )
        )

        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["action"], "retry_deployment")
        self.assertEqual(
            self.calls,
            [
                (
                    "/deployment-runs/18/retry",
                    "POST",
                    {"reason": "Retry after correcting the deployment failure."},
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()