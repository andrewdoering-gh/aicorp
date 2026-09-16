"""
title: AICorp Health Report
version: 0.1.0
author: AICorp
"""

import json
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, Field


class Tools:
    """Read-only Open WebUI tool for the latest AICorp agent report."""

    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        agent_report_url: str = Field(
            default="http://agent:8081/report",
            description="Internal read-only AICorp agent report endpoint.",
        )

    async def aicorp_health_report(self) -> str:
        """
        Return the latest completed AICorp health summary.
        This tool has no write, shell, infrastructure, or external-message access.
        """
        request = urllib.request.Request(self.valves.agent_report_url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status != 200:
                    return f"AICorp report unavailable (HTTP {response.status})."
                payload: dict[str, Any] = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return "No completed AICorp health report is available yet."
            return f"AICorp report unavailable (HTTP {error.code})."
        except (urllib.error.URLError, TimeoutError):
            return "AICorp report unavailable because the agent endpoint could not be reached."

        report = payload.get("report")
        completed_at = payload.get("completed_at", "unknown time")
        if not isinstance(report, str) or not report.strip():
            return "The latest AICorp health report is empty."
        return f"AICorp health report ({completed_at}):\n\n{report}"
