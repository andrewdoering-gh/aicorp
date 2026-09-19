import json
import logging
import os
import urllib.request
from typing import Any


PRODUCT_CONTEXT = """
HomeLabOps is the first commercial product candidate built by AICorp. It targets
technically capable homelab operators who run self-hosted services and need a
clear way to understand service health, active alerts, and the next safe
operational step. The first delivery contract is intentionally concrete:
GOAL-01 is centralized infrastructure visibility and is implemented by the Core
Dashboard MVP, Device Discovery Agent, and Basic Alerting System. GOAL-02 is
routine maintenance automation and GOAL-03 is common-service deployment
simplification; those goals must remain visibly blocked until their own mapped
requirements are implemented and accepted. The Core Dashboard MVP must load
within two seconds, show discovered devices, and display last-known status. The
Device Discovery Agent must detect standard home-server hardware and report
discovery events to the dashboard. The Basic Alerting System must trigger on
defined thresholds and deliver notifications through the configured channel.
The product must not expose secrets, execute arbitrary commands, or bypass
human approval.
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
    "goal_ids",
    "status",
}
VALID_PRIORITIES = {"now", "next", "later"}
VALID_STATUSES = {"proposed", "ready", "in_progress", "blocked", "done"}
GENERATION_TIMEOUT_SECONDS = max(90, int(os.environ.get("AICORP_GENERATION_TIMEOUT_SECONDS", "1800")))
REASONING_MAX_TOKENS = max(16384, int(os.environ.get("AICORP_REASONING_MAX_TOKENS", "65536")))
logger = logging.getLogger("aicorp-agent.product-manager")


class GenerationBudgetExhaustedError(ValueError):
    pass


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
                "goal_ids": _string_list(item["goal_ids"], "goal_ids", maximum=8),
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
        "Use GOAL-01, GOAL-02, and GOAL-03 in each backlog item's goal_ids. "
        "Every Product Brief goal must have at least one mapped backlog item; do not map a requirement to a goal it does not implement.\n\n"
        "Required JSON schema:\n"
        '{"brief":{"title":"string","problem":"string","target_users":["string"],'
        '"goals":["string"],"non_goals":["string"],"assumptions":["string"],'
        '"constraints":["string"],"success_metrics":["string"]},'
        '"backlog":[{"id":"string","title":"string","description":"string",'
        '"priority":"now|next|later","acceptance_criteria":["string"],'
        '"dependencies":["string"],"goal_ids":["GOAL-01"],'
        '"status":"proposed|ready|in_progress|blocked|done"}]}'
    )
    generation_options = (
        {
            "temperature": 0.2,
            "max_tokens": REASONING_MAX_TOKENS,
            "think": True,
            "extra_body": {
                "think": True,
                "chat_template_kwargs": {"enable_thinking": True},
            },
            "response_format": {"type": "json_object"},
        }
        if model != "gpt-5.6-luna"
        else {}
    )
    last_error: Exception | None = None
    for attempt in range(3):
        retry_prompt = prompt
        if last_error is not None:
            retry_prompt += (
                "\n\nYour previous response failed validation with this error: "
                f"{last_error}. Return a complete replacement JSON object. "
                "Every required list, including target_users, goals, non_goals, "
                "assumptions, constraints, success_metrics, and acceptance_criteria, "
                "must contain at least one non-empty string. Every backlog item must also include a non-empty "
                "goal_ids list using GOAL-01, GOAL-02, or GOAL-03. Do not omit or rename fields."
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
                        {"role": "user", "content": retry_prompt},
                    ],
                    **generation_options,
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=GENERATION_TIMEOUT_SECONDS) as response:
                body = json.load(response)
            message = body["choices"][0]["message"]
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ValueError("model response content must be a string")
            content = content.strip()
            response_shape = {
                "finish_reason": body.get("choices", [{}])[0].get("finish_reason"),
                "message_keys": sorted(message),
                "content_length": len(content),
                "reasoning_length": len(message.get("reasoning_content") or message.get("reasoning") or ""),
            }
            if response_shape["finish_reason"] == "length":
                logger.error("model exhausted its output budget; not retrying response_shape=%s", response_shape)
                raise GenerationBudgetExhaustedError(
                    f"model exhausted its output budget: {response_shape}"
                )
            if "</think>" in content:
                content = content.rsplit("</think>", 1)[1].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            if not content:
                raise ValueError("model returned empty final content")
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as error:
                start = content.find("{")
                if start < 0:
                    raise ValueError("model response did not contain a JSON object") from error
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(content[start:])
                except json.JSONDecodeError as decode_error:
                    raise ValueError("model response contained malformed JSON") from decode_error
            return validate_product_output(parsed)
        except GenerationBudgetExhaustedError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as error:
            last_error = error
    raise ValueError(f"product brief generation failed after correction attempts: {last_error}") from last_error
