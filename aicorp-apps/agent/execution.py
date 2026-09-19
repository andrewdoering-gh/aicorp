import re
from typing import Any

from requirements import validate_requirement_contract


EXECUTION_STATUSES = {"ready", "in_progress", "submitted", "qa_passed", "qa_failed", "failed", "completed"}
CHANGE_PROPOSAL_STATUSES = {"proposed", "qa_passed", "qa_failed", "approved", "completed"}


def required_text(value: Any, field_name: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def factual_text(value: Any, field_name: str, maximum: int = 4000) -> str:
    text = required_text(value, field_name, maximum)
    placeholders = {
        "record the actual results of the checks performed.",
        "todo",
        "tbd",
        "to be determined",
    }
    if text.casefold() in placeholders:
        raise ValueError(f"{field_name} must contain factual evidence, not placeholder text")
    return text


def text_list(value: Any, field_name: str, maximum: int = 16) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{field_name} must contain between one and {maximum} strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field_name} must contain non-empty strings")
    return [item.strip() for item in value]


def validate_implementation_submission(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "summary": required_text(payload.get("summary"), "summary"),
        "changed_surfaces": text_list(payload.get("changed_surfaces"), "changed_surfaces"),
        "tests_run": text_list(payload.get("tests_run"), "tests_run"),
        "test_results": factual_text(payload.get("test_results"), "test_results"),
        "diff_reference": required_text(payload.get("diff_reference"), "diff_reference", 1000),
    }
    if payload.get("requirement_contract") is not None:
        normalized["requirement_contract"] = validate_requirement_contract(payload["requirement_contract"])
    if payload.get("remediation") is not None:
        if not isinstance(payload["remediation"], dict):
            raise ValueError("remediation must be an object")
        normalized["remediation"] = payload["remediation"]
    return normalized


def validate_qa_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if result not in {"passed", "failed"}:
        raise ValueError("result must be passed or failed")
    return {
        "result": result,
        "checks": text_list(payload.get("checks"), "checks"),
        "evidence": required_text(payload.get("evidence"), "evidence"),
        "defects": [str(item).strip() for item in payload.get("defects", []) if str(item).strip()],
    }


def _validate_unified_patch(patch: str, expected_files: list[str]) -> str:
    normalized_patch = patch.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized_patch.splitlines()
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("diff --git "):
            if current is not None:
                sections.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
        elif line.strip():
            raise ValueError("patch must begin with a diff header")
    if current is not None:
        sections.append(current)
    if not sections:
        raise ValueError("patch must contain at least one diff header")

    patch_files: list[str] = []
    hunk_pattern = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?$")
    for section in sections:
        match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", section[0])
        if match is None or match[1] != match[2]:
            raise ValueError("patch diff headers must use matching repository paths")
        path = match[1]
        if f"--- a/{path}" not in section or f"+++ b/{path}" not in section:
            raise ValueError("patch must contain matching --- and +++ file headers")
        hunk_indexes = [index for index, line in enumerate(section) if hunk_pattern.fullmatch(line)]
        if not hunk_indexes:
            raise ValueError("patch must contain at least one unified diff hunk")
        changed_lines = [
            line for line in section[hunk_indexes[0] + 1:]
            if (line.startswith("+") or line.startswith("-"))
            and not line.startswith(("+++", "---"))
        ]
        if not changed_lines:
            raise ValueError("patch must contain at least one changed line")
        patch_files.append(path)
    if patch_files != expected_files:
        raise ValueError("patch paths must match the proposal files exactly")
    return normalized_patch


def validate_change_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    files = text_list(payload.get("files"), "files", maximum=20)
    patch = _validate_unified_patch(
        required_text(payload.get("patch"), "patch", maximum=20_000),
        files,
    )
    return {
        "summary": required_text(payload.get("summary"), "summary"),
        "files": files,
        "patch": patch,
        "tests": text_list(payload.get("tests"), "tests"),
    }
