import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from requirements import validate_requirement_contract
from workstreams import WORKSTREAM_FILE_GROUPS, WORKSTREAM_IDS


_GENERATION_LOCK = threading.Lock()
GENERATION_TIMEOUT_SECONDS = max(90, int(os.environ.get("AICORP_GENERATION_TIMEOUT_SECONDS", "1800")))
REASONING_MAX_TOKENS = max(16384, int(os.environ.get("AICORP_REASONING_MAX_TOKENS", "65536")))
PROPOSAL_MAX_TOKENS = max(4096, int(os.environ.get("AICORP_PROPOSAL_MAX_TOKENS", "8192")))
logger = logging.getLogger("aicorp-agent.workers")


PLAN_FIELDS = {
    "workstreams",
    "milestones",
    "dependencies",
    "definition_of_done",
    "test_strategy",
    "risks_and_open_decisions",
    "recommendation",
    "requirement_contract",
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
    "requirement_contract",
}
VALID_PRIORITIES = {"now", "next", "later"}


class GenerationBudgetExhaustedError(ValueError):
    pass


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
        for field in PLAN_FIELDS - {"workstreams", "milestones", "recommendation", "requirement_contract"}
    }
    normalized["requirement_contract"] = validate_requirement_contract(payload["requirement_contract"])
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
    workstream_ids = set()
    for item in workstreams:
        if not isinstance(item, dict) or set(item) != WORKSTREAM_FIELDS:
            raise ValueError("workstream fields do not match the required schema")
        workstream_id = str(item["id"]).strip()
        if workstream_id not in WORKSTREAM_IDS:
            allowed_ids = ", ".join(sorted(WORKSTREAM_IDS))
            raise ValueError(f"workstream id must be one of: {allowed_ids}")
        if workstream_id in workstream_ids:
            raise ValueError(f"workstream id must be unique: {workstream_id}")
        if item["priority"] not in VALID_PRIORITIES:
            raise ValueError("workstream priority must be now, next, or later")
        workstream_ids.add(workstream_id)
        normalized_workstreams.append(
            {
                "id": workstream_id,
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
    for field in WORKER_FIELDS - {"role", "requirement_contract"}:
        normalized[field] = _strings(payload[field], field)
    normalized["requirement_contract"] = validate_requirement_contract(payload["requirement_contract"])
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


def build_qa_worker_plan(engineering_plan: dict[str, Any]) -> dict[str, Any]:
    workstreams = engineering_plan.get("workstreams", [])
    if not isinstance(workstreams, list) or not workstreams:
        raise ValueError("approved engineering plan must contain workstreams for QA planning")
    workstream_ids = [
        str(workstream.get("id", "")).strip()
        for workstream in workstreams
        if isinstance(workstream, dict) and str(workstream.get("id", "")).strip()
    ]
    if not workstream_ids:
        raise ValueError("approved engineering plan workstreams must have IDs for QA planning")
    acceptance_criteria = [
        criterion.strip()
        for workstream in workstreams
        if isinstance(workstream, dict)
        for criterion in workstream.get("acceptance_criteria", [])
        if isinstance(criterion, str) and criterion.strip()
    ]
    test_strategy = engineering_plan.get("test_strategy", [])
    if not isinstance(test_strategy, list):
        test_strategy = []
    risks = engineering_plan.get("risks_and_open_decisions", [])
    if not isinstance(risks, list):
        risks = []
    payload = {
        "role": "qa_engineer",
        "scope": [
            f"Validate execution evidence and acceptance criteria for {workstream_id}."
            for workstream_id in workstream_ids
        ],
        "implementation_steps": [
            "Review each approved workstream and its acceptance criteria.",
            "Verify submitted evidence is factual, bounded, and tied to the current workflow lineage.",
            "Record pass or fail evidence before any repository proposal or deployment gate advances.",
        ],
        "files_or_surfaces": [f"workstream:{workstream_id}" for workstream_id in workstream_ids],
        "tests": (test_strategy or acceptance_criteria or [
            "Verify every approved workstream has execution evidence and acceptance coverage."
        ])[:16],
        "risks": (risks or [
            "Execution evidence may omit an acceptance criterion or reference an unrelated workflow artifact."
        ])[:16],
        "handoff": [
            "Record factual checks and evidence for each submitted task.",
            "Reject evidence that is incomplete, unverifiable, or from another engineering-plan lineage.",
                "If deployment failure evidence is present below, treat it as the defect report: diagnose each failed acceptance criterion, "
                "make the corrective change in the approved surface, and do not merely restate or re-test the previous implementation. "
        ],
        "requirement_contract": engineering_plan["requirement_contract"],
    }
    return validate_worker_output(payload, "qa_engineer")


def _generate(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 4096,
    think: bool = False,
) -> dict[str, Any]:
    generation_options = {
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "think": think,
        "reasoning_effort": "high" if think else "none",
        "extra_body": {
            "think": think,
            "chat_template_kwargs": {"enable_thinking": think},
        },
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                **({} if model == "gpt-5.6-luna" else generation_options),
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    content = ""
    response_shape: dict[str, object] = {}
    for attempt in range(3):
        try:
            with _GENERATION_LOCK:
                with urllib.request.urlopen(request, timeout=GENERATION_TIMEOUT_SECONDS) as response:
                    body = json.load(response)
            choice = body.get("choices", [{}])[0] if isinstance(body, dict) else {}
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            content = message.get("content") or "" if isinstance(message, dict) else ""
            content = content.strip() if isinstance(content, str) else ""
            response_shape = {
                "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
                "message_keys": sorted(message) if isinstance(message, dict) else [],
                "content_length": len(content),
                "reasoning_length": len(
                    message.get("reasoning_content") or message.get("reasoning") or ""
                ) if isinstance(message, dict) else 0,
            }
            if response_shape["finish_reason"] == "length":
                logger.error("model exhausted its output budget; not retrying response_shape=%s", response_shape)
                raise GenerationBudgetExhaustedError(
                    f"model exhausted its output budget: {response_shape}"
                )
            if not content:
                logger.warning(
                    "model returned empty content; retrying attempt=%s response_shape=%s",
                    attempt + 1,
                    response_shape,
                )
                continue
            break
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 2:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = min(30, max(2, int(retry_after))) if retry_after and retry_after.isdigit() else 5 * (attempt + 1)
            time.sleep(delay)
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if not content:
        raise ValueError(f"model returned an empty response: {response_shape}")
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        start = content.find("{")
        if start < 0:
            logger.warning(
                "model returned non-JSON output: length=%s preview=%r",
                len(content),
                content[:300],
            )
            raise ValueError("model response did not contain a JSON object") from error
        try:
            payload, _ = json.JSONDecoder().raw_decode(content[start:])
            return payload
        except json.JSONDecodeError as decode_error:
            logger.warning(
                "model returned malformed JSON: length=%s preview=%r",
                len(content),
                content[:300],
            )
            raise ValueError("model response contained malformed JSON") from decode_error


def generate_engineering_plan(base_url: str, api_key: str, model: str, technical_plan: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "Act as the AICorp Engineering Manager. Convert this approved CTO technical plan into a practical "
        "engineering execution plan. Keep work bounded and assignable. Do not implement code, alter repositories, "
        "deploy infrastructure, or invent missing requirements. Milestones must be structured objects with an id, "
        "title, description, status, and dependencies. Use status 'planned' for new work. Do not use 'complete' "
        "or claim that work is finished unless the approved execution records and QA evidence explicitly prove it. "
        "Workstream IDs must be unique and use only the exact canonical IDs WS-01, WS-02, and WS-03; do not invent "
        "descriptive IDs such as ws-dashboard, ws-agent, or ws-alerting. "
        "Copy the requirement_contract from the approved CTO plan exactly; do not omit, summarize, or alter it. "
        "Return JSON only.\n\n"
        f"Approved CTO plan:\n{json.dumps(technical_plan, indent=2, default=str)}\n\n"
        'Schema: {"workstreams":[{"id":"string","title":"string","description":"string",'
        '"priority":"now|next|later","tasks":["string"],"acceptance_criteria":["string"]}],'
        '"milestones":[{"id":"string","title":"string","description":"string",'
        '"status":"planned|in_progress|blocked|complete","dependencies":["string"]}],'
        '"dependencies":["string"],"definition_of_done":["string"],'
        '"test_strategy":["string"],"risks_and_open_decisions":["string"],"recommendation":"string",'
        '"requirement_contract":{}}'
    )
    system = "You are a careful Engineering Manager. Think through the plan internally, then output valid JSON only."
    payload = _generate(
        base_url,
        api_key,
        model,
        system,
        prompt,
        max_tokens=REASONING_MAX_TOKENS,
        think=True,
    )
    try:
        return validate_engineering_plan(payload)
    except ValueError as error:
        correction_prompt = (
            prompt
            + f"\n\nYour previous response failed validation: {error}. Regenerate the complete JSON response. "
            "Use only the exact unique workstream IDs WS-01, WS-02, and WS-03."
        )
        return validate_engineering_plan(
            _generate(
                base_url,
                api_key,
                model,
                system,
                correction_prompt,
                max_tokens=REASONING_MAX_TOKENS,
                think=True,
            )
        )


def generate_worker_plan(
    base_url: str,
    api_key: str,
    model: str,
    role: str,
    engineering_plan: dict[str, Any],
) -> dict[str, Any]:
    if role == "qa_engineer":
        logger.info("building deterministic QA worker plan from approved engineering plan")
        return build_qa_worker_plan(engineering_plan)
    workstream_ids = [
        str(workstream.get("id", "")).strip()
        for workstream in engineering_plan.get("workstreams", [])
        if isinstance(workstream, dict) and str(workstream.get("id", "")).strip()
    ]
    approved_paths = sorted({
        path
        for workstream_id in workstream_ids
        for path in WORKSTREAM_FILE_GROUPS.get(workstream_id, ())
    })
    available_paths = sorted(_repository_paths())
    scoped_paths = [
        path for path in approved_paths
        if not available_paths or path in available_paths
    ]
    prompt = (
        f"Act as the AICorp {role.replace('_', ' ').title()} worker. The role field must be exactly "
        f"'{role}'. Produce a planning handoff from the "
        "approved Engineering Manager plan. Do not modify code, execute commands, or claim work is complete. "
        "For software_engineer, files_or_surfaces must contain only concrete repository-relative paths beginning "
        "with aicorp/ (for example aicorp/agent/agent.py), never descriptions, directories, wildcards, or container paths. "
        "Choose only the specific files needed for this plan, at most 8 paths. Do not copy the available-file list "
        "into the response and do not return more than 8 files_or_surfaces entries. "
        "Copy the requirement_contract from the approved Engineering Manager plan exactly; do not omit, summarize, or alter it. "
        "Return JSON only.\n\n"
        f"Approved repository files for these workstreams (use only these for software_engineer files_or_surfaces): {scoped_paths}\n\n"
        f"Approved engineering plan:\n{json.dumps(engineering_plan, indent=2, default=str)}\n\n"
        'Schema: {"role":"string","scope":["string"],"implementation_steps":["string"],'
        '"files_or_surfaces":["string"],"tests":["string"],"risks":["string"],"handoff":["string"],'
        '"requirement_contract":{}}'
    )
    system = f"You are a careful {role} worker. Output valid JSON only."
    payload = _generate(base_url, api_key, model, system, prompt, think=False)

    def validate_generated_worker_plan(candidate: Any) -> dict[str, Any]:
        normalized = validate_worker_output(candidate, role)
        if role != "software_engineer":
            return normalized
        selected_paths = set(normalized["files_or_surfaces"])
        invalid_paths = sorted(selected_paths - set(approved_paths))
        uncovered_workstreams = [
            workstream_id
            for workstream_id in workstream_ids
            if not selected_paths.intersection(WORKSTREAM_FILE_GROUPS.get(workstream_id, ()))
        ]
        if invalid_paths or uncovered_workstreams:
            details = []
            if invalid_paths:
                details.append(f"invalid approved-scope paths: {', '.join(invalid_paths)}")
            if uncovered_workstreams:
                details.append(f"uncovered workstreams: {', '.join(uncovered_workstreams)}")
            raise ValueError("files_or_surfaces must cover approved workstreams; " + "; ".join(details))
        return normalized

    try:
        return validate_generated_worker_plan(payload)
    except ValueError as error:
        if role != "software_engineer" or "files_or_surfaces" not in str(error):
            raise
        correction_prompt = (
            prompt
            + "\n\nYour previous response failed validation because files_or_surfaces was invalid. "
            "Regenerate the complete JSON response now. Select no more than 8 individual files from the "
            "approved repository files for the listed workstreams; include at least one file for each workstream. "
            "Do not include directories, prose, or the inventory itself."
        )
        return validate_generated_worker_plan(
            _generate(base_url, api_key, model, system, correction_prompt, think=False),
        )
