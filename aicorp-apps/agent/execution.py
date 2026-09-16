from typing import Any


EXECUTION_STATUSES = {"ready", "in_progress", "submitted", "qa_passed", "qa_failed", "completed"}
CHANGE_PROPOSAL_STATUSES = {"proposed", "qa_passed", "qa_failed", "approved"}


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
    return {
        "summary": required_text(payload.get("summary"), "summary"),
        "changed_surfaces": text_list(payload.get("changed_surfaces"), "changed_surfaces"),
        "tests_run": text_list(payload.get("tests_run"), "tests_run"),
        "test_results": factual_text(payload.get("test_results"), "test_results"),
        "diff_reference": required_text(payload.get("diff_reference"), "diff_reference", 1000),
    }


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


def validate_change_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    files = text_list(payload.get("files"), "files", maximum=20)
    patch = required_text(payload.get("patch"), "patch", maximum=20_000)
    if not patch.startswith("diff --git "):
        raise ValueError("patch must be a unified git diff")
    return {
        "summary": required_text(payload.get("summary"), "summary"),
        "files": files,
        "patch": patch,
        "tests": text_list(payload.get("tests"), "tests"),
    }
