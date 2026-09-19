import copy
import re
from typing import Any


REQUIREMENT_CONTRACT_VERSION = "1.1"
REQUIREMENT_CONTRACT_KEY = "requirement_contract"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _safe_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").upper()
    if not normalized:
        raise ValueError("requirement source id must contain an alphanumeric character")
    return normalized[:48]


def build_requirement_contract(
    brief_id: int,
    brief: dict[str, Any],
    backlog: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(brief_id, int) or brief_id < 1:
        raise ValueError("brief_id must be a positive integer")
    if not isinstance(brief, dict):
        raise ValueError("brief must be an object")
    goals = brief.get("goals")
    if not isinstance(goals, list) or not goals or any(not isinstance(goal, str) or not goal.strip() for goal in goals):
        raise ValueError("brief.goals must contain non-empty strings")
    if not isinstance(backlog, list) or not backlog:
        raise ValueError("backlog must contain at least one item")

    items: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    requirement_ids: set[str] = set()
    for item in backlog:
        if not isinstance(item, dict):
            raise ValueError("backlog items must be objects")
        source_id = _required_text(item.get("id"), "backlog.id")
        normalized_source_id = _safe_identifier(source_id)
        if source_id in source_ids:
            raise ValueError(f"backlog item id must be unique: {source_id}")
        source_ids.add(source_id)
        requirement_id = f"PB-{brief_id}-ITEM-{normalized_source_id}"
        if requirement_id in requirement_ids:
            raise ValueError(f"backlog item ids collide after normalization: {source_id}")
        requirement_ids.add(requirement_id)
        criteria = item.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria or any(
            not isinstance(criterion, str) or not criterion.strip() for criterion in criteria
        ):
            raise ValueError(f"{requirement_id} must contain non-empty acceptance criteria")
        acceptance_criteria = [
            {
                "id": f"{requirement_id}-AC-{index:02d}",
                "text": criterion.strip(),
                "status": "pending",
            }
            for index, criterion in enumerate(criteria, start=1)
        ]
        items.append(
            {
                "id": requirement_id,
                "source_id": source_id,
                "title": _required_text(item.get("title"), f"{requirement_id}.title"),
                "description": _required_text(item.get("description"), f"{requirement_id}.description"),
                "priority": _required_text(item.get("priority"), f"{requirement_id}.priority"),
                "dependencies": [
                    dependency.strip()
                    for dependency in item.get("dependencies", [])
                    if isinstance(dependency, str) and dependency.strip()
                ],
                "status": _required_text(item.get("status"), f"{requirement_id}.status"),
                "acceptance_criteria": acceptance_criteria,
            }
        )

    item_ids = [item["id"] for item in items]
    normalized_goals = []
    goal_aliases = {
        f"GOAL-{index:02d}": f"PB-{brief_id}-GOAL-{index:02d}"
        for index in range(1, len(goals) + 1)
    }
    for index, goal in enumerate(goals, start=1):
        goal_id = f"PB-{brief_id}-GOAL-{index:02d}"
        requirement_ids = []
        for item, requirement_id in zip(backlog, item_ids):
            raw_goal_ids = item.get("goal_ids", [])
            if raw_goal_ids is None:
                raw_goal_ids = []
            if not isinstance(raw_goal_ids, list):
                raise ValueError(f"{requirement_id}.goal_ids must be a list")
            normalized_goal_ids = {str(value).strip() for value in raw_goal_ids if str(value).strip()}
            if any(value not in goal_aliases and value not in goal_aliases.values() for value in normalized_goal_ids):
                raise ValueError(f"{requirement_id}.goal_ids contains an unknown goal")
            if goal_id in normalized_goal_ids or f"GOAL-{index:02d}" in normalized_goal_ids:
                requirement_ids.append(requirement_id)
        normalized_goals.append(
            {
                "id": goal_id,
                "text": goal.strip(),
                "requirement_ids": requirement_ids,
                "status": "pending",
            }
        )
    return {
        "schema_version": REQUIREMENT_CONTRACT_VERSION,
        "brief_id": brief_id,
        "goals": normalized_goals,
        "items": items,
    }


def validate_requirement_contract(
    contract: Any,
    expected_brief_id: int | None = None,
) -> dict[str, Any]:
    if not isinstance(contract, dict) or set(contract) != {"schema_version", "brief_id", "goals", "items"}:
        raise ValueError("requirement contract fields do not match the required schema")
    if contract["schema_version"] != REQUIREMENT_CONTRACT_VERSION:
        raise ValueError("unsupported requirement contract version")
    if not isinstance(contract["brief_id"], int) or contract["brief_id"] < 1:
        raise ValueError("requirement contract brief_id must be a positive integer")
    if expected_brief_id is not None and contract["brief_id"] != expected_brief_id:
        raise ValueError("requirement contract belongs to another product brief")
    goals = contract["goals"]
    items = contract["items"]
    if not isinstance(goals, list) or not goals or not isinstance(items, list) or not items:
        raise ValueError("requirement contract must contain goals and items")
    item_ids = []
    acceptance_ids = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "id", "source_id", "title", "description", "priority", "dependencies", "status", "acceptance_criteria"
        }:
            raise ValueError("requirement item fields do not match the required schema")
        item_id = _required_text(item.get("id"), "requirement item id")
        item_ids.append(item_id)
        criteria = item["acceptance_criteria"]
        if not isinstance(criteria, list) or not criteria:
            raise ValueError(f"{item_id} must contain acceptance criteria")
        for criterion in criteria:
            if not isinstance(criterion, dict) or set(criterion) != {"id", "text", "status"}:
                raise ValueError(f"{item_id} acceptance criterion fields are invalid")
            criterion_id = _required_text(criterion.get("id"), f"{item_id}.acceptance_criteria.id")
            _required_text(criterion.get("text"), f"{criterion_id}.text")
            if criterion.get("status") not in {"pending", "passed", "failed", "blocked"}:
                raise ValueError(f"{criterion_id} status is invalid")
            acceptance_ids.append(criterion_id)
    if len(item_ids) != len(set(item_ids)) or len(acceptance_ids) != len(set(acceptance_ids)):
        raise ValueError("requirement and acceptance criterion IDs must be unique")
    for goal in goals:
        if not isinstance(goal, dict) or set(goal) != {"id", "text", "requirement_ids", "status"}:
            raise ValueError("goal fields do not match the required schema")
        _required_text(goal.get("id"), "goal.id")
        _required_text(goal.get("text"), "goal.text")
        if goal.get("status") not in {"pending", "achieved", "blocked"}:
            raise ValueError("goal status is invalid")
        requirement_ids = goal.get("requirement_ids")
        if (
            not isinstance(requirement_ids, list)
            or len(requirement_ids) != len(set(requirement_ids))
            or any(requirement_id not in item_ids for requirement_id in requirement_ids)
        ):
            raise ValueError("goal requirement_ids must be a unique subset of product requirements")
    return copy.deepcopy(contract)


def attach_requirement_contract(payload: Any, contract: dict[str, Any]) -> dict[str, Any]:
    normalized_contract = validate_requirement_contract(contract)
    if not isinstance(payload, dict):
        raise ValueError("downstream artifact must be an object")
    existing = payload.get(REQUIREMENT_CONTRACT_KEY)
    if existing is not None and validate_requirement_contract(existing) != normalized_contract:
        raise ValueError("downstream artifact requirement contract does not match the Product Brief")
    result = copy.deepcopy(payload)
    result[REQUIREMENT_CONTRACT_KEY] = normalized_contract
    return result


def require_complete_requirement_contract(payload: Any, contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("downstream artifact must be an object")
    expected = validate_requirement_contract(contract)
    actual = payload.get(REQUIREMENT_CONTRACT_KEY)
    if validate_requirement_contract(actual, expected["brief_id"]) != expected:
        raise ValueError("downstream artifact must preserve the complete Product Brief requirement contract")
    return copy.deepcopy(payload)


def acceptance_checks(contract: dict[str, Any]) -> list[dict[str, str]]:
    normalized = validate_requirement_contract(contract)
    return [
        {
            "requirement_id": item["id"],
            "requirement_title": item["title"],
            "acceptance_criterion_id": criterion["id"],
            "text": criterion["text"],
        }
        for item in normalized["items"]
        for criterion in item["acceptance_criteria"]
    ]


def acceptance_evidence_passed(evidence: Any, contract: dict[str, Any]) -> bool:
    normalized = validate_requirement_contract(contract)
    checks = acceptance_checks(normalized)
    if not isinstance(evidence, list) or len(evidence) != len(checks):
        return False
    expected = {check["acceptance_criterion_id"] for check in checks}
    observed = {
        item.get("acceptance_criterion_id")
        for item in evidence
        if isinstance(item, dict) and item.get("result") == "passed"
    }
    evidence_complete = observed == expected and all(
        isinstance(item, dict)
        and isinstance(item.get("command"), str)
        and item["command"].strip()
        and isinstance(item.get("expected"), str)
        and item["expected"].strip()
        and isinstance(item.get("actual"), str)
        and item["actual"].strip()
        and isinstance(item.get("timestamp"), str)
        and item["timestamp"].strip()
        and isinstance(item.get("runtime"), dict)
        and item["runtime"].get("service")
        and item["runtime"].get("version")
        and item["runtime"].get("source_hash")
        for item in evidence
    )
    if not evidence_complete:
        return False
    updated = apply_acceptance_evidence(normalized, evidence)
    return all(item["status"] == "done" for item in updated["items"]) and all(
        goal["status"] == "achieved" for goal in updated["goals"]
    )


def apply_acceptance_evidence(
    contract: dict[str, Any],
    evidence: Any,
) -> dict[str, Any]:
    normalized = validate_requirement_contract(contract)
    if not isinstance(evidence, list):
        raise ValueError("acceptance evidence must be a list")
    evidence_by_id = {
        item.get("acceptance_criterion_id"): item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("acceptance_criterion_id"), str)
    }
    updated = copy.deepcopy(normalized)
    for item in updated["items"]:
        for criterion in item["acceptance_criteria"]:
            result = evidence_by_id.get(criterion["id"], {}).get("result")
            criterion["status"] = "passed" if result == "passed" else "failed"
        item["status"] = "done" if all(
            criterion["status"] == "passed" for criterion in item["acceptance_criteria"]
        ) else "blocked"
    item_statuses = {item["id"]: item["status"] for item in updated["items"]}
    for goal in updated["goals"]:
        goal["status"] = "achieved" if goal["requirement_ids"] and all(
            item_statuses.get(requirement_id) == "done"
            for requirement_id in goal["requirement_ids"]
        ) else "blocked"
    return validate_requirement_contract(updated)