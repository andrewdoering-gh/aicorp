"""
title: AICorp Artifact Inspector
version: 0.1.0
author: AICorp
"""

import json
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field


class Tools:
    """Read-only access to published AICorp planning artifacts."""

    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        agent_url: str = Field(
            default="http://agent:8081",
            description="Internal AICorp agent URL.",
        )
        approval_token: str = Field(
            default="",
            description="Bearer token for authenticated artifact review.",
        )

    def _request(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self.valves.agent_url.rstrip('/')}{path}",
            headers={
                "Authorization": f"Bearer {self.valves.approval_token}",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                return {"error": "Artifact review token was rejected."}
            detail = error.read().decode("utf-8", errors="replace")
            return {"error": f"Artifact API returned HTTP {error.code}: {detail}"}
        except (urllib.error.URLError, TimeoutError):
            return {"error": "Artifact API is unavailable."}

    async def get_latest_product_brief(self) -> str:
        """Show the latest persisted Product Manager brief, publication status, and backlog."""
        return json.dumps(self._request("/product-briefs/latest"), indent=2, default=str)

    async def list_product_briefs(self, include_archived: bool = False) -> str:
        """List product briefs; archived briefs are excluded unless explicitly requested."""
        path = "/product-briefs?include_archived=true" if include_archived else "/product-briefs"
        return json.dumps(self._request(path), indent=2, default=str)

    async def get_latest_technical_plan(self) -> str:
        """Show the latest persisted CTO technical plan."""
        return json.dumps(self._request("/technical-plans/latest"), indent=2, default=str)

    async def get_latest_engineering_plan(self) -> str:
        """Show the latest persisted Engineering Manager plan."""
        return json.dumps(self._request("/engineering-plans/latest"), indent=2, default=str)

    async def list_worker_plans(self) -> str:
        """Show persisted Software Engineer and QA worker plans."""
        return json.dumps(self._request("/worker-plans"), indent=2, default=str)
