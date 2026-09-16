import json
import urllib.request
from typing import Any


CTO_REQUIRED_FIELDS = {
    "feasibility",
    "architecture",
    "data_and_api_contracts",
    "security_model",
    "deployment_model",
    "failure_and_recovery",
    "non_functional_requirements",
    "engineering_backlog",
    "risks_and_open_decisions",
    "recommendation",
}
TECHNICAL_BACKLOG_FIELDS = {
    "id",
    "title",
    "description",
    "priority",
    "acceptance_criteria",
    "dependencies",
}
VALID_PRIORITIES = {"now", "next", "later"}


def _string_list(value: Any, field_name: str, maximum: int = 16) -> list[str]:
    if isinstance(value, str) and value.strip():
        value = [value]
    elif value is not None and not isinstance(value, list):
        value = [json.dumps(value, sort_keys=True)]
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field_name} must be a list of at most {maximum} strings")
    normalized = []
    for item in value:
        if not isinstance(item, str):
            item = json.dumps(item, sort_keys=True)
        if item.strip():
            normalized.append(item.strip())
    return normalized


def validate_technical_plan(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != CTO_REQUIRED_FIELDS:
        keys = sorted(payload) if isinstance(payload, dict) else []
        raise ValueError(f"technical plan fields do not match the required schema; keys={keys}")
    normalized = {}
    for field in (
        "feasibility",
        "architecture",
        "data_and_api_contracts",
        "security_model",
        "deployment_model",
        "failure_and_recovery",
        "non_functional_requirements",
        "risks_and_open_decisions",
    ):
        normalized[field] = _string_list(payload[field], field)
    if not isinstance(payload["recommendation"], str) or not payload["recommendation"].strip():
        raise ValueError("recommendation must be a non-empty string")
    backlog = payload["engineering_backlog"]
    if not isinstance(backlog, list) or not 1 <= len(backlog) <= 16:
        raise ValueError("engineering_backlog must contain between one and sixteen items")
    normalized_backlog = []
    for item in backlog:
        if not isinstance(item, dict) or set(item) != TECHNICAL_BACKLOG_FIELDS:
            raise ValueError("technical backlog item fields do not match the required schema")
        if not isinstance(item["id"], str) or not item["id"].strip():
            raise ValueError("technical backlog item id is required")
        if not isinstance(item["title"], str) or not item["title"].strip():
            raise ValueError("technical backlog item title is required")
        if item["priority"] not in VALID_PRIORITIES:
            raise ValueError("technical backlog priority must be now, next, or later")
        normalized_backlog.append(
            {
                "id": item["id"].strip(),
                "title": item["title"].strip(),
                "description": str(item["description"]).strip(),
                "priority": item["priority"],
                "acceptance_criteria": _string_list(item["acceptance_criteria"], "acceptance_criteria"),
                "dependencies": _string_list(item["dependencies"], "dependencies"),
            }
        )
    normalized["engineering_backlog"] = normalized_backlog
    normalized["recommendation"] = payload["recommendation"].strip()
    return normalized


def generate_technical_plan(
    base_url: str,
    api_key: str,
    model: str,
    product_brief: dict[str, Any],
) -> dict[str, Any]:
    prompt = (
        "Act as the AICorp CTO. Evaluate the approved product brief below and "
        "produce a practical technical plan for HomeLabOps. Use only the brief "
        "and known platform constraints. Do not invent customer validation. "
        "Prefer the existing Docker Compose, Python agent, PostgreSQL, "
        "Prometheus, and Open WebUI platform. Do not propose autonomous shell "
        "access or unrestricted infrastructure changes. Return JSON only.\n\n"
        "Approved product brief and any explicitly required dependencies:\n"
        f"{json.dumps(product_brief, indent=2, default=str)}\n\n"
        "Required JSON fields:\n"
        '{"feasibility":["string"],"architecture":["string"],'
        '"data_and_api_contracts":["string"],"security_model":["string"],'
        '"deployment_model":["string"],"failure_and_recovery":["string"],'
        '"non_functional_requirements":["string"],"engineering_backlog":['
        '{"id":"string","title":"string","description":"string",'
        '"priority":"now|next|later","acceptance_criteria":["string"],'
        '"dependencies":["string"]}],"risks_and_open_decisions":["string"],'
        '"recommendation":"string"}'
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a careful CTO. Output valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                **({} if model == "gpt-5.6-luna" else {"temperature": 0.2}),
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        content = json.load(response)["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    decoded, _ = json.JSONDecoder().raw_decode(content)
    return validate_technical_plan(decoded)
