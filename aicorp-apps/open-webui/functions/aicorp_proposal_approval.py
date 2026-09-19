"""
title: AICorp Proposal Approval
version: 0.4.0
author: AICorp
"""

import json
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field


class Tools:
    """Read repository proposals and request an automatically approved deployment retry.

    This tool is inspection and retry-request only. Retry requests are approved
    automatically after the agent validates the failed deployment and proposal lineage.
    """

    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        agent_url: str = Field(default="http://agent:8081", description="Internal AICorp agent URL.")
        approval_token: str = Field(default="", description="Bearer token for the approval API.")

    def _request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{self.valves.agent_url.rstrip('/')}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
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

    async def get_latest_change_proposal(self) -> str:
        """Show the latest repository change proposal, patch, QA result, and status."""
        return json.dumps(self._request("/change-proposals/latest"), indent=2, default=str)

    async def get_change_proposal(self, proposal_id: int) -> str:
        """Show one repository change proposal by its numeric ID."""
        return json.dumps(self._request(f"/change-proposals/{proposal_id}"), indent=2, default=str)

    async def list_change_proposals(self) -> str:
        """List repository change proposals without replacing older records with the latest one."""
        return json.dumps(self._request("/change-proposals"), indent=2, default=str)

    async def request_proposal_approval(self, proposal_id: int = 2) -> str:
        """Explain that QA automatically approves proposals; no human request is created."""
        return json.dumps(
            {
                "error": "repository-change approval is automatic after QA passes",
                "next_step": f"Wait for the QA Engineer to approve proposal {proposal_id} and create the human deploy approval.",
            },
            indent=2,
        )

    async def request_deployment_retry(
        self,
        source_approval_id: int,
        reason: str,
    ) -> str:
        """MUTATING: create an automatically approved retry for one failed deployment.

        This creates and approves a new governed retry request. The deployment
        worker starts it after the validated request is persisted.
        """
        if not isinstance(reason, str) or not reason.strip():
            return json.dumps({"error": "retry reason is required"}, indent=2)
        result = self._request(
            f"/deployment-runs/{source_approval_id}/retry",
            method="POST",
            payload={"reason": reason},
        )
        if isinstance(result, dict) and result.get("action") == "retry_deployment":
            result["next_step"] = "The validated retry request is approved automatically and will be picked up by the deployment worker."
        return json.dumps(result, indent=2, default=str)

    async def approve_repository_proposal(self, proposal_id: int = 2, approval_id: int = 0) -> str:
        """Compatibility wrapper; QA automatically approves proposals and creates deploy approval."""
        return json.dumps(
            {
                "error": "human approval is not used for repository proposals",
                "next_step": f"Wait for QA to automatically approve proposal {proposal_id}; then approve its deploy request.",
            },
            indent=2,
        )

    async def supersede_repository_proposal(self, proposal_id: int, reason: str) -> str:
        """Supersede an approved proposal when its evidence and patch do not match."""
        result = self._request(
            f"/change-proposals/{proposal_id}/supersede",
            method="POST",
            payload={"reason": reason},
        )
        return json.dumps(result, indent=2, default=str)
