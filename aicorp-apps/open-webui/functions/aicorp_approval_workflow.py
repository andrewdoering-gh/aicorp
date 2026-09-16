"""
title: AICorp Approval Workflow
version: 0.1.0
author: AICorp
"""

import json
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field


class Tools:
    """Authenticated approval-ledger tool; it never executes approved actions.

    Archive commands must call the archive_product_brief tool. Do not claim an
    approval request exists unless this tool returns an approval ID.
    """

    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        agent_url: str = Field(
            default="http://agent:8081",
            description="Internal AICorp agent URL.",
        )
        approval_token: str = Field(
            default="",
            description="Bearer token for the authenticated approval ledger API.",
        )
        agent_approval_token: str = Field(
            default="",
            description="Bearer token for the configured agent generation approver.",
        )
        agent_name: str = Field(
            default="",
            description="Configured agent identity used for generation approvals.",
        )

    def _request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.valves.agent_url.rstrip('/')}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.valves.approval_token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                return {"error": "Approval token was rejected."}
            detail = error.read().decode("utf-8", errors="replace")
            return {"error": f"Approval API returned HTTP {error.code}: {detail}"}
        except (urllib.error.URLError, TimeoutError):
            return {"error": "Approval API is unavailable."}

    async def list_pending_approvals(self) -> str:
        """List pending approval records for human review; no action is executed."""
        result = self._request("/approval-requests")
        if "error" in result:
            return result["error"]
        pending = [item for item in result.get("requests", []) if item.get("status") == "pending"]
        if not pending:
            return "There are no pending approval requests."
        return json.dumps(pending, indent=2, default=str)

    async def request_approval(
        self,
        action: str,
        requested_by: str,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Create a pending approval record; this does not execute the requested action."""
        payload = {
            "action": action,
            "requested_by": requested_by,
            "reason": reason,
        }
        if context:
            payload["context"] = context
        result = self._request(
            "/approval-requests",
            method="POST",
            payload=payload,
        )
        return json.dumps(result, indent=2, default=str)

    async def request_repository_change_proposal_submission(self, reason: str) -> str:
        """Create the approval request required before the Software Engineer submits a repository proposal."""
        result = self._request(
            "/approval-requests",
            method="POST",
            payload={
                "action": "submit_repository_change_proposal",
                "requested_by": "software_engineer",
                "reason": reason,
            },
        )
        return json.dumps(result, indent=2, default=str)

    async def decide_approval(
        self,
        request_id: int,
        status: str,
        decided_by: str,
        decision_reason: str,
    ) -> str:
        """Record an explicit human approve/deny decision; it never executes an action."""
        if status not in {"approved", "denied"}:
            return "status must be approved or denied"
        payload = {
            "status": status,
            "decision_reason": decision_reason,
        }
        if status == "approved":
            payload["decided_by"] = decided_by
        result = self._request(
            f"/approval-requests/{request_id}",
            method="POST",
            payload=payload,
        )
        return json.dumps(result, indent=2, default=str)

    async def approve_generation_request(self, request_id: int, decision_reason: str) -> str:
        """Approve one permitted artifact-generation request as this configured agent."""
        if not self.valves.agent_approval_token or not self.valves.agent_name:
            return "agent_approval_token and agent_name must be configured"
        result = self._request_with_token(
            f"/approval-requests/{request_id}",
            method="POST",
            payload={
                "status": "approved",
                "decided_by": self.valves.agent_name,
                "decision_reason": decision_reason,
            },
            token=self.valves.agent_approval_token,
        )
        return json.dumps(result, indent=2, default=str)

    async def retry_approved_generation(self, request_id: int) -> str:
        """Retry an approved automatic generation request after a dispatch failure."""
        result = self._request(
            f"/approval-requests/{request_id}/retry",
            method="POST",
            payload={},
        )
        return json.dumps(result, indent=2, default=str)

    def _request_with_token(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        token: str = "",
    ) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.valves.agent_url.rstrip('/')}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                return {"error": "Approval token was rejected."}
            detail = error.read().decode("utf-8", errors="replace")
            return {"error": f"Approval API returned HTTP {error.code}: {detail}"}
        except (urllib.error.URLError, TimeoutError):
            return {"error": "Approval API is unavailable."}

    async def execute_approved_health_report(self, approval_id: int) -> str:
        """Execute only an approved read_health_report request; no other action is supported."""
        result = self._request(
            "/refresh-health-report",
            method="POST",
            payload={"approval_id": approval_id},
        )
        return json.dumps(result, indent=2, default=str)

    async def generate_approved_product_brief(
        self,
        approval_id: int,
        requested_by: str = "operator",
        product_context: str = "",
    ) -> str:
        """Generate a structured PM brief from an automatically queued handoff."""
        payload = {"approval_id": approval_id, "requested_by": requested_by}
        if product_context:
            payload["product_context"] = product_context
        result = self._request("/product-briefs/generate", method="POST", payload=payload)
        return json.dumps(result, indent=2, default=str)

    async def request_product_brief(
        self,
        reason: str = "Create a new HomeLabOps product brief.",
        product_context: str = "",
    ) -> str:
        """Queue automatic PM brief generation; no generation approval is required.

        The returned request ID is an approved internal handoff. Do not send
        it to approve_generation_request or describe it as a pending approval.
        Publication of the generated draft remains the human approval gate.
        """
        payload = {"requested_by": "andrew", "reason": reason}
        if product_context:
            payload["product_context"] = product_context
        result = self._request("/product-briefs/request", method="POST", payload=payload)
        if isinstance(result, dict) and result.get("status") == "queued" and isinstance(result.get("id"), int):
            return json.dumps(
                {
                    **result,
                    "action": "generate_product_brief",
                    "approval_status": "automatically_approved",
                    "next_step": "Wait for the Product Manager to generate the draft; publication will require human approval.",
                },
                indent=2,
                default=str,
            )
        return json.dumps(result, indent=2, default=str)

    async def approve_product_brief(
        self,
        brief_id: int,
        approval_id: int,
        decided_by: str = "operator",
    ) -> str:
        """Compatibility endpoint for publishing a brief with an approved request."""
        result = self._request(
            f"/product-briefs/{brief_id}/approve",
            method="POST",
            payload={"approval_id": approval_id, "decided_by": decided_by},
        )
        return json.dumps(result, indent=2, default=str)

    async def archive_product_brief(
        self,
        brief_id: int,
        reason: str,
        confirm: bool = False,
    ) -> str:
        """Submit archival and return the created approval ID; never archive directly.

        A successful response always contains a numeric ``id`` and
        ``status=pending``. If the API returns an error or no ID, report that
        no approval request was created.
        """
        result = self._request(
            f"/product-briefs/{brief_id}/archive",
            method="POST",
            payload={"reason": reason, "confirm": confirm},
        )
        if not isinstance(result, dict) or not isinstance(result.get("id"), int) or result.get("status") != "pending":
            return json.dumps(
                {
                    "error": "archive approval request was not created",
                    "api_response": result,
                },
                indent=2,
                default=str,
            )
        return json.dumps(result, indent=2, default=str)

    async def generate_approved_technical_plan(
        self,
        product_brief_id: int,
        approval_id: int,
        requested_by: str = "operator",
    ) -> str:
        """Generate a CTO plan only from an approved product brief and approval."""
        result = self._request(
            "/technical-plans/generate",
            method="POST",
            payload={
                "product_brief_id": product_brief_id,
                "approval_id": approval_id,
                "requested_by": requested_by,
            },
        )
        return json.dumps(result, indent=2, default=str)

    async def approve_technical_plan(
        self,
        plan_id: int,
        approval_id: int,
        decided_by: str = "operator",
    ) -> str:
        """Publish a CTO plan only after a separate approve_technical_plan approval."""
        result = self._request(
            f"/technical-plans/{plan_id}/approve",
            method="POST",
            payload={"approval_id": approval_id, "decided_by": decided_by},
        )
        return json.dumps(result, indent=2, default=str)

    async def request_technical_plan_amendment(
        self,
        technical_plan_id: int,
        dependencies: list[str],
        reason: str = "Record newly identified platform and test-data dependencies.",
    ) -> str:
        """Request a governed CTO amendment with explicit dependencies."""
        result = self._request(
            "/technical-plans/amend",
            method="POST",
            payload={
                "technical_plan_id": technical_plan_id,
                "dependencies": dependencies,
                "requested_by": "andrew",
                "reason": reason,
            },
        )
        return json.dumps(result, indent=2, default=str)

    async def generate_approved_engineering_plan(
        self,
        technical_plan_id: int,
        approval_id: int,
        requested_by: str = "operator",
    ) -> str:
        """Generate an Engineering Manager plan from an approved CTO plan."""
        result = self._request(
            "/engineering-plans/generate",
            method="POST",
            payload={"technical_plan_id": technical_plan_id, "approval_id": approval_id, "requested_by": requested_by},
        )
        return json.dumps(result, indent=2, default=str)

    async def approve_engineering_plan(self, plan_id: int, approval_id: int, decided_by: str = "operator") -> str:
        """Approve an Engineering Manager plan after separate human approval."""
        result = self._request(
            f"/engineering-plans/{plan_id}/approve",
            method="POST",
            payload={"approval_id": approval_id, "decided_by": decided_by},
        )
        return json.dumps(result, indent=2, default=str)

    async def generate_approved_worker_plan(
        self,
        engineering_plan_id: int,
        approval_id: int,
        role: str,
        requested_by: str = "operator",
    ) -> str:
        """Generate a Software Engineer or QA plan from an approved EM plan."""
        result = self._request(
            "/worker-plans/generate",
            method="POST",
            payload={
                "engineering_plan_id": engineering_plan_id,
                "approval_id": approval_id,
                "role": role,
                "requested_by": requested_by,
            },
        )
        return json.dumps(result, indent=2, default=str)

    async def approve_worker_plan(self, plan_id: int, approval_id: int, decided_by: str = "operator") -> str:
        """Approve a worker plan after role-specific human approval."""
        result = self._request(
            f"/worker-plans/{plan_id}/approve",
            method="POST",
            payload={"approval_id": approval_id, "decided_by": decided_by},
        )
        return json.dumps(result, indent=2, default=str)

    async def start_approved_software_engineer_execution(
        self,
        worker_plan_id: int,
        work_item_id: str,
        approval_id: int,
        requested_by: str = "operator",
    ) -> str:
        """Start one approved work item from an approved Software Engineer plan."""
        result = self._request(
            "/execution-tasks/start",
            method="POST",
            payload={
                "worker_plan_id": worker_plan_id,
                "work_item_id": work_item_id,
                "approval_id": approval_id,
                "requested_by": requested_by,
            },
        )
        return json.dumps(result, indent=2, default=str)

    async def submit_software_engineer_execution(
        self,
        task_id: int,
        approval_id: int,
        summary: str,
        changed_surfaces: list[str],
        tests_run: list[str],
        test_results: str,
        diff_reference: str,
    ) -> str:
        """Submit implementation evidence for one started work item."""
        result = self._request(
            f"/execution-tasks/{task_id}/submit",
            method="POST",
            payload={
                "approval_id": approval_id,
                "summary": summary,
                "changed_surfaces": changed_surfaces,
                "tests_run": tests_run,
                "test_results": test_results,
                "diff_reference": diff_reference,
            },
        )
        return json.dumps(result, indent=2, default=str)

    async def record_approved_qa_validation(
        self,
        task_id: int,
        qa_plan_id: int,
        approval_id: int,
        result: str,
        checks: list[str],
        evidence: str,
        defects: list[str] | None = None,
    ) -> str:
        """Record QA pass/fail evidence from an approved QA worker plan."""
        response = self._request(
            f"/execution-tasks/{task_id}/qa",
            method="POST",
            payload={
                "qa_plan_id": qa_plan_id,
                "approval_id": approval_id,
                "result": result,
                "checks": checks,
                "evidence": evidence,
                "defects": defects or [],
            },
        )
        return json.dumps(response, indent=2, default=str)

    async def list_execution_tasks(self) -> str:
        """List governed Software Engineer execution tasks and QA results."""
        result = self._request("/execution-tasks")
        if "error" in result:
            return result["error"]
        return json.dumps(result, indent=2, default=str)

    async def submit_repository_change_proposal(
        self,
        task_id: int,
        worker_plan_id: int,
        approval_id: int,
        summary: str,
        files: list[str],
        patch: str,
        tests: list[str],
        requested_by: str = "operator",
    ) -> str:
        """Submit one bounded unified-diff proposal after its approval request is approved."""
        result = self._request(
            f"/execution-tasks/{task_id}/change-proposal",
            method="POST",
            payload={
                "worker_plan_id": worker_plan_id,
                "approval_id": approval_id,
                "summary": summary,
                "files": files,
                "patch": patch,
                "tests": tests,
                "requested_by": requested_by,
            },
        )
        return json.dumps(result, indent=2, default=str)

    async def submit_software_engineer_repository_change_proposal(
        self,
        task_id: int,
        worker_plan_id: int,
        approval_id: int,
        summary: str,
        files: list[str],
        patch: str,
        tests: list[str],
    ) -> str:
        """Submit a proposal as the Software Engineer; this never applies, commits, or deploys it."""
        return await self.submit_repository_change_proposal(
            task_id=task_id,
            worker_plan_id=worker_plan_id,
            approval_id=approval_id,
            summary=summary,
            files=files,
            patch=patch,
            tests=tests,
            requested_by="software_engineer",
        )

    async def get_latest_change_proposal(self) -> str:
        """Read the latest proposed repository change and QA status."""
        return json.dumps(self._request("/change-proposals/latest"), indent=2, default=str)

    async def get_change_proposal(self, proposal_id: int) -> str:
        """Read one repository change proposal by its numeric ID."""
        return json.dumps(self._request(f"/change-proposals/{proposal_id}"), indent=2, default=str)

    async def list_change_proposals(self) -> str:
        """List repository change proposals, including older proposals."""
        return json.dumps(self._request("/change-proposals"), indent=2, default=str)

    async def review_repository_change(
        self,
        proposal_id: int,
        qa_plan_id: int,
        approval_id: int,
        result: str,
        checks: list[str],
        evidence: str,
        defects: list[str] | None = None,
    ) -> str:
        """Record QA review of a proposed diff; this never applies the patch."""
        response = self._request(
            f"/change-proposals/{proposal_id}/qa",
            method="POST",
            payload={
                "qa_plan_id": qa_plan_id,
                "approval_id": approval_id,
                "result": result,
                "checks": checks,
                "evidence": evidence,
                "defects": defects or [],
            },
        )
        return json.dumps(response, indent=2, default=str)

    async def approve_repository_change(self, proposal_id: int, approval_id: int, decided_by: str = "operator") -> str:
        """Compatibility wrapper; repository-change approval is QA-owned and automatic."""
        return json.dumps(
            {
                "error": "human approval is not used for repository changes",
                "next_step": "The QA Engineer automatically approves a QA-passed proposal and creates the human deploy approval.",
            },
            indent=2,
        )
