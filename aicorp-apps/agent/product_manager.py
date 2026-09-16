import json
import urllib.request
from typing import Any


PRODUCT_CONTEXT = """
HomeLabOps is the first commercial product candidate built by AICorp. It targets
technically capable homelab operators who run self-hosted services and need a
clear way to understand service health, active alerts, and the next safe
operational step. The current prototype is read-only and combines Prometheus
health, active alerts, and an AICorp health-summary report. It must not expose
secrets, execute arbitrary commands, or bypass human approval.
""".strip()

BRIEF_REQUIRED_FIELDS = {
    "title",
    "problem",
    "target_users",
    "goals",
    "non_goals",
    "assumptions",
    "constraints",
    "success_metrics",
}
BACKLOG_REQUIRED_FIELDS = {
    "id",
    "title",
    "description",
    "priority",
    "acceptance_criteria",
    "dependencies",
    "status",
}
VALID_PRIORITIES = {"now", "next", "later"}
VALID_STATUSES = {"proposed", "ready", "in_progress", "blocked", "done"}


def _string_list(value: Any, field_name: str, maximum: int = 12) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} must be a non-empty list of at most {maximum} strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field_name} must contain non-empty strings")
    return [item.strip() for item in value]


def validate_product_output(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"brief", "backlog"}:
        raise ValueError("model output must contain only brief and backlog")
    brief = payload["brief"]
    backlog = payload["backlog"]
    if not isinstance(brief, dict) or set(brief) != BRIEF_REQUIRED_FIELDS:
        raise ValueError("brief fields do not match the required product schema")
    for field in ("title", "problem"):
        if not isinstance(brief[field], str) or not brief[field].strip():
            raise ValueError(f"brief.{field} must be a non-empty string")
    for field in ("target_users", "goals", "non_goals", "assumptions", "constraints", "success_metrics"):
        _string_list(brief[field], f"brief.{field}")
    if not isinstance(backlog, list) or not 1 <= len(backlog) <= 12:
        raise ValueError("backlog must contain between one and twelve items")
    normalized_backlog = []
    for item in backlog:
        if not isinstance(item, dict) or set(item) != BACKLOG_REQUIRED_FIELDS:
            raise ValueError("backlog item fields do not match the required schema")
        if not isinstance(item["id"], str) or not item["id"].strip():
            raise ValueError("backlog item id must be a non-empty string")
        if not isinstance(item["title"], str) or not item["title"].strip():
            raise ValueError("backlog item title must be a non-empty string")
        if item["priority"] not in VALID_PRIORITIES:
            raise ValueError("backlog priority must be now, next, or later")
        if item["status"] not in VALID_STATUSES:
            raise ValueError("backlog status is invalid")
        dependencies = item["dependencies"]
        if dependencies in (None, ""):
            dependencies = []
        if not isinstance(dependencies, list):
            dependencies = []
        dependencies = [dependency.strip() for dependency in dependencies if isinstance(dependency, str) and dependency.strip()]
        normalized_backlog.append(
            {
                "id": item["id"].strip(),
                "title": item["title"].strip(),
                "description": str(item["description"]).strip(),
                "priority": item["priority"],
                "acceptance_criteria": _string_list(item["acceptance_criteria"], "acceptance_criteria"),
                "dependencies": _string_list(dependencies, "dependencies", maximum=8)
                if dependencies
                else [],
                "status": item["status"],
            }
        )
    return {"brief": brief, "backlog": normalized_backlog}


def generate_product_output(
    base_url: str,
    api_key: str,
    model: str,
    context: str,
) -> dict[str, Any]:
    prompt = (
        "Act as the AICorp Product Manager. Produce a commercially grounded "
        "product brief and engineering backlog for HomeLabOps. Use only the "
        "provided context. Do not invent customer research or claim demand has "
        "been validated. Return JSON only, with exactly the requested schema. "
        "Keep the first backlog small enough for a prototype.\n\n"
        f"Product context:\n{context}\n\n"
        "Required JSON schema:\n"
        '{"brief":{"title":"string","problem":"string","target_users":["string"],'
        '"goals":["string"],"non_goals":["string"],"assumptions":["string"],'
        '"constraints":["string"],"success_metrics":["string"]},'
        '"backlog":[{"id":"string","title":"string","description":"string",'
        '"priority":"now|next|later","acceptance_criteria":["string"],'
        '"dependencies":["string"],"status":"proposed|ready|in_progress|blocked|done"}]}'
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a careful product manager. Output valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                **({} if model == "gpt-5.6-luna" else {"temperature": 0.2}),
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        body = json.load(response)
    content = body["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return validate_product_output(json.loads(content))
