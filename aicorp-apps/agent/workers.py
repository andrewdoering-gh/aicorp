import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any


PLAN_FIELDS = {
    "workstreams",
    "milestones",
    "dependencies",
    "definition_of_done",
    "test_strategy",
    "risks_and_open_decisions",
    "recommendation",
}
WORKSTREAM_FIELDS = {"id", "title", "description", "priority", "tasks", "acceptance_criteria"}
MILESTONE_FIELDS = {"id", "title", "description", "status", "dependencies"}
MILESTONE_STATUSES = {"planned", "in_progress", "blocked", "complete"}
WORKER_FIELDS = {
    "role",
    "scope",
    "implementation_steps",
    "files_or_surfaces",
    "tests",
    "risks",
    "handoff",
}
VALID_PRIORITIES = {"now", "next", "later"}


def _repository_paths() -> set[str]:
    root = Path(os.environ.get("AICORP_REPOSITORY_ROOT", "/workspace/repository"))
    if not root.is_dir():
        return set()
    return {
        "aicorp/" + str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file() and ".env" not in path.name and not any(part in {"data", "logs"} for part in path.relative_to(root).parts)
    }


def _strings(value: Any, name: str, maximum: int = 16) -> list[str]:
    if isinstance(value, str) and value.strip():
        value = [value]
    elif value is not None and not isinstance(value, list):
        value = [json.dumps(value, sort_keys=True)]
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a list of at most {maximum} strings")
    normalized = []
    for item in value:
        if not isinstance(item, str):
            item = json.dumps(item, sort_keys=True)
        if item.strip():
            normalized.append(item.strip())
    return normalized


def validate_engineering_plan(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != PLAN_FIELDS:
        raise ValueError("engineering plan fields do not match the required schema")
    normalized = {
        field: _strings(payload[field], field)
        for field in PLAN_FIELDS - {"workstreams", "milestones", "recommendation"}
    }
    milestones = payload["milestones"]
    if not isinstance(milestones, list) or not 1 <= len(milestones) <= 16:
        raise ValueError("milestones must contain between one and sixteen items")
    normalized_milestones = []
    for item in milestones:
        if not isinstance(item, dict) or set(item) != MILESTONE_FIELDS:
            raise ValueError("milestone fields do not match the required schema")
        milestone_id = str(item["id"]).strip()
        title = str(item["title"]).strip()
        description = str(item["description"]).strip()
        if not milestone_id or not title or not description:
            raise ValueError("milestone id, title, and description are required")
        if item["status"] not in MILESTONE_STATUSES:
            raise ValueError("milestone status is invalid")
        dependencies = _strings(item["dependencies"], "milestone.dependencies")
        normalized_milestones.append(
            {
                "id": milestone_id,
                "title": title,
                "description": description,
                "status": item["status"],
                "dependencies": dependencies,
            }
        )
    normalized["milestones"] = normalized_milestones
    workstreams = payload["workstreams"]
    if not isinstance(workstreams, list) or not 1 <= len(workstreams) <= 16:
        raise ValueError("workstreams must contain between one and sixteen items")
    normalized_workstreams = []
    for item in workstreams:
        if not isinstance(item, dict) or set(item) != WORKSTREAM_FIELDS:
            raise ValueError("workstream fields do not match the required schema")
        if item["priority"] not in VALID_PRIORITIES:
            raise ValueError("workstream priority must be now, next, or later")
        normalized_workstreams.append(
            {
                "id": str(item["id"]).strip(),
                "title": str(item["title"]).strip(),
                "description": str(item["description"]).strip(),
                "priority": item["priority"],
                "tasks": _strings(item["tasks"], "tasks"),
                "acceptance_criteria": _strings(item["acceptance_criteria"], "acceptance_criteria"),
            }
        )
    normalized["workstreams"] = normalized_workstreams
    if not isinstance(payload["recommendation"], str) or not payload["recommendation"].strip():
        raise ValueError("recommendation must be a non-empty string")
    normalized["recommendation"] = payload["recommendation"].strip()
    return normalized


def validate_worker_output(payload: Any, expected_role: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != WORKER_FIELDS:
        raise ValueError("worker output fields do not match the required schema")
    role = payload["role"]
    role_aliases = {
        "software_engineer": {"software_engineer", "Software Engineer"},
        "qa_engineer": {"qa_engineer", "QA Engineer", "Quality Assurance Engineer"},
    }
    if role not in role_aliases.get(expected_role, {expected_role}):
        raise ValueError(f"worker role must be {expected_role}")
    if expected_role == "software_engineer" and isinstance(payload.get("files_or_surfaces"), list):
        available = _repository_paths()
        if available:
            payload = dict(payload)
            payload["files_or_surfaces"] = [
                path
                for path in payload["files_or_surfaces"]
                if isinstance(path, str)
                and path in available
                and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", path)
            ][:8]
    normalized = {"role": expected_role}
    for field in WORKER_FIELDS - {"role"}:
        normalized[field] = _strings(payload[field], field)
    if expected_role == "software_engineer":
        paths = normalized["files_or_surfaces"]
        if not paths or any(
            not path.startswith("aicorp/")
            or ".." in path.split("/")
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", path)
            for path in paths
        ):
            raise ValueError("software_engineer files_or_surfaces must contain concrete aicorp repository paths")
        available = _repository_paths()
        if available and any(path not in available for path in paths):
            raise ValueError("software_engineer files_or_surfaces must reference existing repository files")
    return normalized


def _generate(base_url: str, api_key: str, model: str, system: str, prompt: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                **({} if model == "gpt-5.6-luna" else {"temperature": 0.2}),
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        content = json.load(response)["choices"][0]["message"]["content"].strip()
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        if start < 0:
            raise
        payload, _ = json.JSONDecoder().raw_decode(content[start:])
        return payload


def generate_engineering_plan(base_url: str, api_key: str, model: str, technical_plan: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "Act as the AICorp Engineering Manager. Convert this approved CTO technical plan into a practical "
        "engineering execution plan. Keep work bounded and assignable. Do not implement code, alter repositories, "
        "deploy infrastructure, or invent missing requirements. Milestones must be structured objects with an id, "
        "title, description, status, and dependencies. Use status 'planned' for new work. Do not use 'complete' "
        "or claim that work is finished unless the approved execution records and QA evidence explicitly prove it. "
        "Return JSON only.\n\n"
        f"Approved CTO plan:\n{json.dumps(technical_plan, indent=2, default=str)}\n\n"
        'Schema: {"workstreams":[{"id":"string","title":"string","description":"string",'
        '"priority":"now|next|later","tasks":["string"],"acceptance_criteria":["string"]}],'
        '"milestones":[{"id":"string","title":"string","description":"string",'
        '"status":"planned|in_progress|blocked|complete","dependencies":["string"]}],'
        '"dependencies":["string"],"definition_of_done":["string"],'
        '"test_strategy":["string"],"risks_and_open_decisions":["string"],"recommendation":"string"}'
    )
    return validate_engineering_plan(
        _generate(base_url, api_key, model, "You are a careful Engineering Manager. Output valid JSON only.", prompt)
    )


def generate_worker_plan(
    base_url: str,
    api_key: str,
    model: str,
    role: str,
    engineering_plan: dict[str, Any],
) -> dict[str, Any]:
    available_paths = sorted(_repository_paths())
    prompt = (
        f"Act as the AICorp {role.replace('_', ' ').title()} worker. The role field must be exactly "
        f"'{role}'. Produce a planning handoff from the "
        "approved Engineering Manager plan. Do not modify code, execute commands, or claim work is complete. "
        "For software_engineer, files_or_surfaces must contain only concrete repository-relative paths beginning "
        "with aicorp/ (for example aicorp/agent/agent.py), never descriptions, directories, wildcards, or container paths. "
        "Choose only the specific files needed for this plan, at most 8 paths. Do not copy the available-file list "
        "into the response and do not return more than 8 files_or_surfaces entries. "
        "Return JSON only.\n\n"
        f"Available repository files (use only these for software_engineer files_or_surfaces): {available_paths}\n\n"
        f"Approved engineering plan:\n{json.dumps(engineering_plan, indent=2, default=str)}\n\n"
        'Schema: {"role":"string","scope":["string"],"implementation_steps":["string"],'
        '"files_or_surfaces":["string"],"tests":["string"],"risks":["string"],"handoff":["string"]}'
    )
    system = f"You are a careful {role} worker. Output valid JSON only."
    payload = _generate(base_url, api_key, model, system, prompt)
    try:
        return validate_worker_output(payload, role)
    except ValueError as error:
        if role != "software_engineer" or "files_or_surfaces" not in str(error):
            raise
        correction_prompt = (
            prompt
            + "\n\nYour previous response failed validation because files_or_surfaces was invalid. "
            "Regenerate the complete JSON response now. Select no more than 8 individual files from the "
            "available repository files; do not include directories, prose, or the inventory itself."
        )
        return validate_worker_output(_generate(base_url, api_key, model, system, correction_prompt), role)
