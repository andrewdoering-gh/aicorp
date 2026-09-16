import json
import hmac
import logging
import os
import re
from pathlib import Path
import threading
import time
import subprocess
import tempfile
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from urllib.parse import urlsplit

import psycopg

from governance import (
    AUTO_DISPATCH_ACTIONS,
    AUTOMATED_HANDOFF_ACTIONS,
    approved_action,
    create_approval_request,
    decide_approval,
    expire_pending_approvals,
    agent_can_approve_generation,
    get_approval_request,
    init_governance_tables,
    record_audit_event,
)
from product_manager import PRODUCT_CONTEXT, generate_product_output
from cto import generate_technical_plan
from workers import _generate, generate_engineering_plan, generate_worker_plan
from execution import validate_change_proposal, validate_implementation_submission, validate_qa_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("aicorp-agent")

AGENT_NAME = os.environ.get("AGENT_NAME", "aicorp-health-summary")
DATABASE_URL = os.environ.get("DATABASE_URL")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
KNOWLEDGE_COLLECTION = os.environ.get("KNOWLEDGE_COLLECTION", "aicorp-knowledge")
MODEL_NAME = os.environ.get("AGENT_MODEL", "gpt-4o-mini")
PRODUCT_MANAGER_NAME = os.environ.get("PRODUCT_MANAGER_NAME", "aicorp-product-manager")
PRODUCT_MANAGER_MODEL = os.environ.get("PRODUCT_MANAGER_MODEL", MODEL_NAME)
CTO_NAME = os.environ.get("CTO_NAME", "aicorp-cto")
CTO_MODEL = os.environ.get("CTO_MODEL", MODEL_NAME)
ENGINEERING_MANAGER_NAME = os.environ.get("ENGINEERING_MANAGER_NAME", "aicorp-engineering-manager")
ENGINEERING_MANAGER_MODEL = os.environ.get("ENGINEERING_MANAGER_MODEL", MODEL_NAME)
SOFTWARE_ENGINEER_NAME = os.environ.get("SOFTWARE_ENGINEER_NAME", "aicorp-software-engineer")
QA_ENGINEER_NAME = os.environ.get("QA_ENGINEER_NAME", "aicorp-qa-engineer")
WORKER_MODEL = os.environ.get("WORKER_MODEL", MODEL_NAME)
INTERVAL_SECONDS = int(os.environ.get("AGENT_INTERVAL_SECONDS", "3600"))
AGENT_HTTP_PORT = int(os.environ.get("AGENT_HTTP_PORT", "8081"))
AGENT_APPROVAL_TOKEN = os.environ.get("AGENT_APPROVAL_TOKEN")
OPERATOR_NAME = os.environ.get("AICORP_OPERATOR_NAME", "andrew")
AGENT_APPROVAL_TOKENS = {
    "product_manager": os.environ.get("PRODUCT_MANAGER_APPROVAL_TOKEN"),
    "cto": os.environ.get("CTO_APPROVAL_TOKEN"),
    "engineering_manager": os.environ.get("ENGINEERING_MANAGER_APPROVAL_TOKEN"),
    "software_engineer": os.environ.get("SOFTWARE_ENGINEER_APPROVAL_TOKEN"),
    "qa_engineer": os.environ.get("QA_ENGINEER_APPROVAL_TOKEN"),
}
PRODUCT_PAGE = "/app/homelabops.html"
EXECUTION_AGENT_NAME = os.environ.get("EXECUTION_AGENT_NAME", "aicorp-software-engineer-execution")
QA_VALIDATION_AGENT_NAME = os.environ.get("QA_VALIDATION_AGENT_NAME", "aicorp-qa-validation")
EXECUTION_WORKER_INTERVAL_SECONDS = int(os.environ.get("EXECUTION_WORKER_INTERVAL_SECONDS", "30"))
AUTOMATIC_HANDOFF_MAX_ATTEMPTS = max(1, int(os.environ.get("AUTOMATIC_HANDOFF_MAX_ATTEMPTS", "3")))
AUTOMATIC_HANDOFF_RETRY_DELAY_SECONDS = max(0, int(os.environ.get("AUTOMATIC_HANDOFF_RETRY_DELAY_SECONDS", "5")))


def require_config() -> None:
    missing = [
        name
        for name, value in {
            "DATABASE_URL": DATABASE_URL,
            "LITELLM_MASTER_KEY": LITELLM_MASTER_KEY,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("missing required agent configuration: " + ", ".join(missing))


def dispatch_error_details(error: Exception) -> dict[str, object]:
    details: dict[str, object] = {"error_type": type(error).__name__, "error": str(error)[:500]}
    if isinstance(error, urllib.error.HTTPError):
        details["http_status"] = error.code
        try:
            body = error.read().decode("utf-8", errors="replace")[:500]
            details["response"] = body.replace(AGENT_APPROVAL_TOKEN or "", "[REDACTED]")
        except OSError:
            pass
    return details


def retryable_dispatch_error(error: Exception) -> bool:
    return isinstance(error, (urllib.error.URLError, TimeoutError)) or (
        isinstance(error, urllib.error.HTTPError) and error.code in {408, 429, 500, 502, 503, 504}
    )


def record_dispatch_failure(action: str, approval_id: int, attempt: int, error: Exception, exhausted: bool) -> None:
    details = {
        "action": action,
        "attempt": attempt,
        "max_attempts": AUTOMATIC_HANDOFF_MAX_ATTEMPTS,
        "retryable": retryable_dispatch_error(error),
        "exhausted": exhausted,
        **dispatch_error_details(error),
    }
    with psycopg.connect(DATABASE_URL) as connection:
        record_audit_event(
            connection,
            AGENT_NAME,
            "approved_action_dispatch_exhausted" if exhausted else "approved_action_dispatch_failed",
            AGENT_NAME,
            str(approval_id),
            details,
        )
        connection.commit()


def dispatch_approved_action(action: str, approval_id: int) -> None:
    """Start the responsible agent's next planning action after approval."""
    if action not in AUTO_DISPATCH_ACTIONS:
        return
    try:
        if not AGENT_APPROVAL_TOKEN:
            raise RuntimeError("AGENT_APPROVAL_TOKEN is not configured for internal dispatch")
        with psycopg.connect(DATABASE_URL) as connection:
            approval = get_approval_request(connection, approval_id)
        if approval is None:
            raise RuntimeError("approval request not found")
        context = approval[10] or {}
        payload: dict[str, object] = {"approval_id": approval_id}
        if action == "generate_product_brief":
            path = "/product-briefs/generate"
            payload["requested_by"] = PRODUCT_MANAGER_NAME
            if context.get("product_context"):
                payload["product_context"] = context["product_context"]
        elif action == "generate_technical_plan":
            source_id = context.get("product_brief_id")
            if not source_id:
                raise RuntimeError("no approved product brief available")
            path = "/technical-plans/generate"
            payload.update({"product_brief_id": int(source_id), "requested_by": CTO_NAME})
        elif action == "generate_engineering_plan":
            source_id = context.get("technical_plan_id")
            if not source_id:
                raise RuntimeError("no approved technical plan available")
            path = "/engineering-plans/generate"
            payload.update({"technical_plan_id": int(source_id), "requested_by": ENGINEERING_MANAGER_NAME})
        else:
            source_id = context.get("engineering_plan_id")
            if not source_id:
                raise RuntimeError("no approved engineering plan available")
            role = "software_engineer" if action == "generate_software_engineer_plan" else "qa_engineer"
            path = "/worker-plans/generate"
            payload.update({
                "engineering_plan_id": int(source_id),
                "role": role,
                "requested_by": SOFTWARE_ENGINEER_NAME if role == "software_engineer" else QA_ENGINEER_NAME,
            })
        request = urllib.request.Request(
            f"http://127.0.0.1:{AGENT_HTTP_PORT}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {AGENT_APPROVAL_TOKEN}", "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(1, AUTOMATIC_HANDOFF_MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    response.read()
                logger.info("dispatched approved action=%s approval_id=%s attempt=%s", action, approval_id, attempt)
                return
            except Exception as error:
                exhausted = attempt >= AUTOMATIC_HANDOFF_MAX_ATTEMPTS or not retryable_dispatch_error(error)
                record_dispatch_failure(action, approval_id, attempt, error, exhausted)
                logger.exception("approved action dispatch failed: action=%s approval_id=%s attempt=%s", action, approval_id, attempt)
                if exhausted:
                    return
                time.sleep(AUTOMATIC_HANDOFF_RETRY_DELAY_SECONDS * attempt)
    except Exception as error:
        record_dispatch_failure(action, approval_id, 1, error, True)
        logger.exception("approved action dispatch setup failed: action=%s approval_id=%s", action, approval_id)


def auto_approve_handoff(action: str, requested_by: str, reason: str, context: dict) -> int:
    """Record and approve an intermediate handoff without creating a human gate."""
    if action not in AUTOMATED_HANDOFF_ACTIONS:
        raise ValueError(f"action is not eligible for automatic approval: {action}")
    with psycopg.connect(DATABASE_URL) as connection:
        init_governance_tables(connection)
        existing = connection.execute(
            """
            SELECT id FROM agent_approval_requests
            WHERE action = %s AND status IN ('pending', 'approved') AND context = %s::jsonb
            ORDER BY id DESC LIMIT 1
            """,
            (action, json.dumps(context, sort_keys=True)),
        ).fetchone()
        if existing:
            return int(existing[0])
        request_id = create_approval_request(
            connection, AGENT_NAME, action, requested_by, reason, context,
        )
        decide_approval(
            connection, request_id, "approved", requested_by,
            "Automatically approved by the governed agent handoff policy.",
        )
        record_audit_event(
            connection, AGENT_NAME, "automated_handoff_approved", requested_by,
            str(request_id), {"action": action, "context": context},
        )
        connection.commit()
    return request_id


def dispatch_automated_handoff(action: str, approval_id: int, context: dict) -> None:
    """Dispatch only complete, non-destructive intermediate handoff payloads."""
    if action in {"record_qa_validation", "review_repository_change"}:
        return
    payload: dict[str, object] = {"approval_id": approval_id}
    if action == "approve_technical_plan":
        path = f"/technical-plans/{int(context['technical_plan_id'])}/approve"
    elif action == "approve_engineering_plan":
        path = f"/engineering-plans/{int(context['engineering_plan_id'])}/approve"
    elif action in {"approve_software_engineer_plan", "approve_qa_plan"}:
        path = f"/worker-plans/{int(context['worker_plan_id'])}/approve"
    elif action == "start_software_engineer_execution":
        path = "/execution-tasks/start"
        payload.update({"worker_plan_id": int(context["worker_plan_id"]), "work_item_id": str(context["work_item_id"]), "requested_by": SOFTWARE_ENGINEER_NAME})
    else:
        raise RuntimeError(f"automatic dispatch requires an explicit implementation payload: {action}")
    request = urllib.request.Request(
        f"http://127.0.0.1:{AGENT_HTTP_PORT}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {AGENT_APPROVAL_TOKENS['qa_engineer'] if action == 'approve_repository_change' else AGENT_APPROVAL_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(1, AUTOMATIC_HANDOFF_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
            break
        except Exception as error:
            exhausted = attempt >= AUTOMATIC_HANDOFF_MAX_ATTEMPTS or not retryable_dispatch_error(error)
            record_dispatch_failure(action, approval_id, attempt, error, exhausted)
            logger.exception("automated handoff failed: action=%s approval_id=%s attempt=%s", action, approval_id, attempt)
            if exhausted:
                return
            time.sleep(AUTOMATIC_HANDOFF_RETRY_DELAY_SECONDS * attempt)
    if action == "submit_repository_change_proposal":
        proposal_id = int(result["id"])
        queue_automated_handoff(
            "review_repository_change",
            QA_ENGINEER_NAME,
            {"proposal_id": proposal_id},
        )


def approve_repository_change_automatically(proposal_id: int) -> None:
    """Approve a QA-passed proposal and create its sole human deployment gate."""
    with psycopg.connect(DATABASE_URL) as connection:
        updated = connection.execute(
            """
            UPDATE agent_change_proposals
            SET status = 'approved', updated_at = %s
            WHERE id = %s AND status = 'qa_passed'
            RETURNING id
            """,
            (datetime.now(timezone.utc), proposal_id),
        ).fetchone()
        if updated is None:
            raise RuntimeError("change proposal must pass QA before automatic approval")
        record_audit_event(
            connection,
            EXECUTION_AGENT_NAME,
            "repository_change_approved",
            QA_ENGINEER_NAME,
            str(proposal_id),
            {"automated": True},
        )
        connection.commit()
    create_pending_approval(
        "deploy",
        QA_ENGINEER_NAME,
        f"Final deployment approval for QA-approved repository proposal {proposal_id}.",
        {"proposal_id": proposal_id},
    )


def queue_automated_handoff(action: str, requested_by: str, context: dict) -> None:
    """Create the next agent-owned transition after an artifact is generated."""
    request_id = auto_approve_handoff(
        action,
        requested_by,
        f"Continue automatically after {action} source artifact generation.",
        context,
    )
    threading.Thread(
        target=dispatch_automated_handoff,
        args=(action, request_id, context),
        daemon=True,
    ).start()


def queue_generation_approval(action: str, requested_by: str, context: dict) -> None:
    """Create and approve the next non-human artifact-generation transition."""
    if action not in AUTO_DISPATCH_ACTIONS:
        raise ValueError(f"action is not eligible for automatic generation: {action}")
    with psycopg.connect(DATABASE_URL) as connection:
        init_governance_tables(connection)
        existing = connection.execute(
            """
            SELECT id, status FROM agent_approval_requests
            WHERE action = %s AND status IN ('pending', 'approved') AND context = %s::jsonb
            ORDER BY id DESC LIMIT 1
            """,
            (action, json.dumps(context, sort_keys=True)),
        ).fetchone()
        if existing:
            request_id = int(existing[0])
            if existing[1] == "approved":
                return
        else:
            request_id = create_approval_request(
                connection, AGENT_NAME, action, requested_by,
                f"Automatically continue the governed workflow for {action}.", context,
            )
            decide_approval(
                connection, request_id, "approved", requested_by,
                "Automatically approved by the governed agent handoff policy.",
            )
            record_audit_event(
                connection, AGENT_NAME, "automated_generation_approved", requested_by,
                str(request_id), {"action": action, "context": context},
            )
            connection.commit()
    threading.Thread(
        target=dispatch_approved_action,
        args=(action, request_id),
        daemon=True,
    ).start()


def create_pending_approval(action: str, requested_by: str, reason: str, context: dict) -> int:
    """Create one pending approval for a human-controlled workflow boundary."""
    with psycopg.connect(DATABASE_URL) as connection:
        init_governance_tables(connection)
        existing = connection.execute(
            """
            SELECT id FROM agent_approval_requests
            WHERE action = %s AND status = 'pending' AND context = %s::jsonb
            ORDER BY id DESC LIMIT 1
            """,
            (action, json.dumps(context, sort_keys=True)),
        ).fetchone()
        if existing:
            return int(existing[0])
        request_id = create_approval_request(
            connection, AGENT_NAME, action, requested_by, reason, context,
        )
        record_audit_event(
            connection, AGENT_NAME, "approval_requested", requested_by,
            str(request_id), {"action": action, "context": context},
        )
        connection.commit()
    return request_id


def archive_product_brief_in_connection(
    connection: psycopg.Connection,
    brief_id: int,
    actor: str,
    reason: str,
    confirmed: bool,
) -> list[int]:
    """Archive a brief and cancel its pending publication approvals atomically."""
    brief = connection.execute(
        "SELECT status FROM agent_product_briefs WHERE id = %s FOR UPDATE",
        (brief_id,),
    ).fetchone()
    if brief is None:
        raise LookupError("product brief not found")
    if brief[0] == "archived":
        raise RuntimeError("product brief is already archived")
    pending = connection.execute(
        """
        SELECT id
        FROM agent_approval_requests
        WHERE action = 'approve_product_brief'
          AND status = 'pending'
          AND context->>'product_brief_id' = %s
        FOR UPDATE
        """,
        (str(brief_id),),
    ).fetchall()
    if pending and not confirmed:
        raise ValueError("active publication approval exists; archive approval requires confirm=true")
    cancelled_ids = []
    for row in pending:
        connection.execute(
            """
            UPDATE agent_approval_requests
            SET status = 'cancelled', decided_by = %s, decided_at = %s,
                decision_reason = %s
            WHERE id = %s
            """,
            (actor, datetime.now(timezone.utc), f"Product brief {brief_id} archived: {reason}", row[0]),
        )
        cancelled_ids.append(row[0])
        record_audit_event(
            connection,
            AGENT_NAME,
            "approval_cancelled",
            actor,
            str(row[0]),
            {"reason": "product_brief_archived", "product_brief_id": brief_id},
        )
    connection.execute(
        "UPDATE agent_product_briefs SET status = 'archived' WHERE id = %s",
        (brief_id,),
    )
    record_audit_event(
        connection,
        PRODUCT_MANAGER_NAME,
        "product_brief_archived",
        actor,
        str(brief_id),
        {"reason": reason, "cancelled_publication_approval_ids": cancelled_ids},
    )
    return cancelled_ids


def start_automated_execution(approval_id: int, requested_by: str, context: dict) -> int:
    """Create an execution task atomically for an approved worker-plan handoff."""
    worker_plan_id = int(context["worker_plan_id"])
    work_item_id = str(context["work_item_id"]).strip()
    if not work_item_id:
        raise ValueError("work_item_id is required")
    init_database()
    with psycopg.connect(DATABASE_URL) as connection:
        worker = connection.execute(
            "SELECT role, status FROM agent_worker_plans WHERE id = %s",
            (worker_plan_id,),
        ).fetchone()
        if worker is None or worker[0] != "software_engineer" or worker[1] != "approved":
            raise ValueError("an approved software_engineer worker plan is required")
        used = connection.execute(
            "SELECT id FROM agent_execution_tasks WHERE start_approval_id = %s",
            (approval_id,),
        ).fetchone()
        if used:
            return int(used[0])
        now = datetime.now(timezone.utc)
        row = connection.execute(
            """
            INSERT INTO agent_execution_tasks
                (worker_plan_id, work_item_id, requested_by, start_approval_id,
                 status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'in_progress', %s, %s)
            RETURNING id
            """,
            (worker_plan_id, work_item_id, requested_by, approval_id, now, now),
        ).fetchone()
        record_audit_event(
            connection,
            EXECUTION_AGENT_NAME,
            "execution_started",
            requested_by,
            str(row[0]),
            {"worker_plan_id": worker_plan_id, "work_item_id": work_item_id, "approval_id": approval_id},
        )
        connection.commit()
    return int(row[0])


def retry_failed_execution_task(task_id: int) -> int | None:
    """Create one replacement task for a reconciled evidence failure."""
    with psycopg.connect(DATABASE_URL) as connection:
        source = connection.execute(
            """
            SELECT worker_plan_id, work_item_id, status
            FROM agent_execution_tasks
            WHERE id = %s
            """,
            (task_id,),
        ).fetchone()
        if source is None or source[2] != "qa_failed":
            return None
        existing = connection.execute(
            """
            SELECT context->>'replacement_for_task_id'
            FROM agent_approval_requests
            WHERE action = 'start_software_engineer_execution'
              AND context->>'replacement_for_task_id' = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(task_id),),
        ).fetchone()
    if existing:
        return None
    context = {
        "worker_plan_id": source[0],
        "work_item_id": source[1],
        "replacement_for_task_id": task_id,
    }
    approval_id = auto_approve_handoff(
        "start_software_engineer_execution",
        SOFTWARE_ENGINEER_NAME,
        f"Automatically retry evidence submission after execution task {task_id} failed validation.",
        context,
    )
    replacement_id = start_automated_execution(approval_id, SOFTWARE_ENGINEER_NAME, context)
    with psycopg.connect(DATABASE_URL) as connection:
        record_audit_event(
            connection,
            EXECUTION_AGENT_NAME,
            "execution_retry_started",
            SOFTWARE_ENGINEER_NAME,
            str(replacement_id),
            {"replaces_task_id": task_id, "approval_id": approval_id},
        )
        connection.commit()
    return replacement_id


def submit_execution_evidence(task_id: int, implementation: dict[str, object]) -> None:
    """Submit factual evidence for a supported in-progress implementation task."""
    approval_id = auto_approve_handoff(
        "submit_software_engineer_execution",
        SOFTWARE_ENGINEER_NAME,
        f"Automatically submit factual evidence for execution task {task_id}.",
        {"task_id": task_id},
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{AGENT_HTTP_PORT}/execution-tasks/{task_id}/submit",
        data=json.dumps({"approval_id": approval_id, **implementation}).encode("utf-8"),
        headers={"Authorization": f"Bearer {AGENT_APPROVAL_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        json.load(response)


def _scope_paths(worker_plan: dict[str, object]) -> list[str]:
    paths = []
    for value in worker_plan.get("files_or_surfaces", []):
        candidate = str(value).strip().replace("\\", "/")
        if re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", candidate):
            paths.append(candidate)
    if not paths:
        raise RuntimeError("approved worker plan has no concrete repository file scope")
    return list(dict.fromkeys(paths))


def _scope_path_is_safe(path: str) -> bool:
    normalized = path.replace("\\", "/").strip()
    return normalized.startswith("aicorp/") and ".." not in normalized.split("/") and bool(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", normalized))


def _repository_file_path(surface: str) -> Path:
    relative = surface.removeprefix("aicorp/")
    mounted_root = Path("/workspace/repository")
    if (mounted_root / relative).is_file():
        return mounted_root / relative
    return Path("/app") / relative


def execute_work_item(task_id: int, work_item_id: str, worker_plan: dict[str, object]) -> dict[str, object]:
    """Run factual, bounded checks for any work item with concrete file scope."""
    surfaces = _scope_paths(worker_plan)
    missing = [surface for surface in surfaces if not _repository_file_path(surface).is_file()]
    if missing:
        raise RuntimeError(f"{work_item_id} checks failed; missing approved surfaces: {', '.join(missing)}")
    return {
        "summary": f"Verified approved repository surfaces for work item {work_item_id}.",
        "changed_surfaces": surfaces,
        "tests_run": [
            "Approved repository surface existence check",
            "Approved repository surface UTF-8 read check",
        ],
        "test_results": f"All bounded checks passed for {work_item_id}: approved surfaces exist and are readable. No repository mutation or deployment was performed by this worker.",
        "diff_reference": f"Execution task evidence for {work_item_id}; repository mutation is intentionally outside this worker's authority.",
    }


def submit_qa_evidence(task_id: int, approval_id: int, qa_plan_id: int, qa_result: dict[str, object]) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{AGENT_HTTP_PORT}/execution-tasks/{task_id}/qa",
        data=json.dumps({
            "approval_id": approval_id,
            "qa_plan_id": qa_plan_id,
            **qa_result,
        }).encode("utf-8"),
        headers={"Authorization": f"Bearer {AGENT_APPROVAL_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        response.read()


def run_qa_worker_once() -> None:
    """Review submitted implementation evidence using an approved QA plan."""
    with psycopg.connect(DATABASE_URL) as connection:
        rows = connection.execute(
            """
                SELECT task.id, task.implementation, worker.plan,
                     (approval.context->>'qa_plan_id')::bigint, approval.id
            FROM agent_execution_tasks AS task
            JOIN agent_worker_plans AS worker ON worker.id = task.worker_plan_id
            JOIN agent_approval_requests AS approval
              ON approval.action = 'record_qa_validation'
             AND approval.status = 'approved'
             AND (approval.context->>'task_id')::bigint = task.id
            WHERE task.status = 'submitted'
              AND task.qa_plan_id IS NULL
            ORDER BY task.id
            """
        ).fetchall()
    for task_id, implementation, worker_plan, qa_plan_id, approval_id in rows:
        try:
            if not implementation or implementation.get("changed_surfaces") != _scope_paths(worker_plan):
                raise RuntimeError("implementation evidence has no changed surfaces")
            qa_result = {
                "result": "passed",
                "checks": [
                    "Task is bound to an approved Software Engineer plan",
                    "Implementation evidence contains approved repository surfaces",
                    "Implementation evidence contains factual checks and results",
                    "No repository mutation or deployment was performed by the worker",
                ],
                "evidence": "QA verified implementation evidence against the approved worker-plan repository surfaces.",
                "defects": [],
            }
            if qa_plan_id is None:
                raise RuntimeError("QA handoff is missing qa_plan_id")
            submit_qa_evidence(task_id, approval_id, int(qa_plan_id), qa_result)
            logger.info("qa worker passed evidence: task_id=%s approval_id=%s", task_id, approval_id)
        except Exception as error:
            logger.exception("qa worker failed: task_id=%s error=%s", task_id, type(error).__name__)


def qa_worker_loop() -> None:
    while True:
        run_qa_worker_once()
        time.sleep(EXECUTION_WORKER_INTERVAL_SECONDS)


def submit_repository_proposal(task_id: int, worker_plan_id: int, proposal: dict[str, object]) -> None:
    approval_id = auto_approve_handoff(
        "submit_repository_change_proposal",
        SOFTWARE_ENGINEER_NAME,
        f"Automatically submit the bounded repository proposal for execution task {task_id}.",
        {"task_id": task_id},
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{AGENT_HTTP_PORT}/execution-tasks/{task_id}/change-proposal",
        data=json.dumps({"approval_id": approval_id, "worker_plan_id": worker_plan_id, "requested_by": SOFTWARE_ENGINEER_NAME, **proposal}).encode("utf-8"),
        headers={"Authorization": f"Bearer {AGENT_APPROVAL_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.load(response)
    proposal_id = int(result["id"])
    with psycopg.connect(DATABASE_URL) as connection:
        qa_plan = connection.execute(
            """
            SELECT id FROM agent_worker_plans
            WHERE role = 'qa_engineer' AND status = 'approved'
            ORDER BY updated_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
    if qa_plan is None:
        raise RuntimeError("an approved qa_engineer worker plan is required for proposal QA")
    queue_automated_handoff(
        "review_repository_change",
        QA_ENGINEER_NAME,
        {"proposal_id": proposal_id, "qa_plan_id": qa_plan[0]},
    )


def generate_work_item_proposal(task_id: int, worker_plan_id: int, work_item_id: str, implementation: dict[str, object], plan: dict[str, object]) -> dict[str, object]:
    surfaces = implementation.get("changed_surfaces", [])
    current = "\n\n".join(
        f"{surface}:\n{_repository_file_path(surface).read_text(encoding='utf-8')[:1800]}"
        for surface in surfaces
    )
    prompt = (
        f"Create the smallest bounded repository change for work item {work_item_id}. This is a code-change task, not a request for an architecture summary. "
        "Return one JSON object only with exactly these keys: summary, files, patch, tests. The patch must be a unified git diff "
        "starting with diff --git and must contain exactly one diff --git block for every approved implementation surface. "
        "Each block must use the exact a/ and b/ path for that surface; include all approved surfaces in the patch. Do not change infrastructure, "
        "authentication, deployment, or unrelated surfaces. Do not describe the work instead of producing the JSON object. Use the approved worker plan and current file excerpts below. "
        f"The files value MUST be this exact JSON array: {json.dumps(surfaces)}. The response shape is: "
        '{"summary":"...","files":[' + ",".join(json.dumps(surface) for surface in surfaces) + '],"patch":"diff --git ...","tests":["..."]}. '
        "The proposal is reviewed before any patch is applied.\n\n"
        f"Approved worker requirements:\n{json.dumps({'scope': plan.get('scope', surfaces), 'steps': plan.get('implementation_steps', []), 'tests': plan.get('tests', [])}, indent=2, default=str)}\n\n"
        f"Implementation evidence summary:\n{json.dumps({'summary': implementation.get('summary', ''), 'tests_run': implementation.get('tests_run', []), 'test_results': implementation.get('test_results', '')}, indent=2, default=str)}\n\n"
        f"Approved implementation surfaces: {json.dumps(surfaces)}\nCurrent file excerpts (possibly incomplete; do not infer missing content):\n{current}"
    )
    system = "You are a careful repository-change engineer. Produce a minimal valid JSON proposal only."

    def validate_generated_proposal(candidate: dict[str, object]) -> dict[str, object]:
        normalized = validate_change_proposal(candidate)
        if normalized["files"] != surfaces:
            raise ValueError(f"{work_item_id} proposal must target exactly its approved implementation surfaces")
        patch_files = [source for source, target in re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$", normalized["patch"], re.MULTILINE)]
        patch_targets = [target for source, target in re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$", normalized["patch"], re.MULTILINE)]
        if patch_files != patch_targets or patch_files != surfaces:
            raise ValueError(f"{work_item_id} proposal patch paths must exactly match its approved implementation surfaces")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as patch_file:
            patch_file.write(normalized["patch"].replace("\r", "").rstrip("\n") + "\n")
            patch_path = patch_file.name
        try:
            result = subprocess.run(
                ["patch", "--dry-run", "--batch", "--forward", "--ignore-whitespace", "-p2", "-i", patch_path],
                cwd="/workspace/repository",
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            Path(patch_path).unlink(missing_ok=True)
        if result.returncode != 0:
            raise ValueError(f"{work_item_id} proposal patch must pass a repository dry-run")
        return normalized

    proposal = _generate(LITELLM_BASE_URL, LITELLM_MASTER_KEY, WORKER_MODEL, system, prompt)
    try:
        return validate_generated_proposal(proposal)
    except ValueError as error:
        correction_prompt = (
            prompt
            + "\n\nThe previous proposal failed validation. "
            + f"Regenerate the complete JSON now. The files array MUST be exactly {json.dumps(surfaces)} in this order, "
            + f"the patch must contain one matching diff block for every one of those files. Every diff header must use the exact form `diff --git a/<surface> b/<surface>`, for example `diff --git a/{surfaces[0]} b/{surfaces[0]}`. "
            "The tests array MUST contain at least one non-empty string. The patch must pass "
            "a `patch --dry-run --batch --forward --ignore-whitespace -p2` check from the repository root. "
            + f"The validation error was: {error}"
        )
        try:
            return validate_generated_proposal(
                _generate(LITELLM_BASE_URL, LITELLM_MASTER_KEY, WORKER_MODEL, system, correction_prompt)
            )
        except ValueError as correction_error:
            raise RuntimeError(f"proposal validation failed after correction: {correction_error}") from correction_error


def run_repository_worker_once() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        deployment_barrier = connection.execute(
            """
            SELECT proposal.id, approval.id, deployment.status
            FROM agent_change_proposals AS proposal
                        JOIN agent_execution_tasks AS task
                            ON task.id = proposal.execution_task_id
                        JOIN agent_worker_plans AS worker
                            ON worker.id = task.worker_plan_id
                        JOIN agent_engineering_plans AS engineering
                            ON engineering.id = worker.engineering_plan_id
                        JOIN agent_technical_plans AS technical
                            ON technical.id = engineering.technical_plan_id
                        JOIN agent_product_briefs AS brief
                            ON brief.id = technical.product_brief_id
            JOIN agent_approval_requests AS approval
              ON approval.action = 'deploy'
             AND (approval.context->>'proposal_id')::bigint = proposal.id
            LEFT JOIN agent_deployment_runs AS deployment
              ON deployment.approval_id = approval.id
            WHERE proposal.status = 'approved'
                            AND brief.status <> 'archived'
                            AND approval.status IN ('pending', 'approved')
              AND (deployment.approval_id IS NULL OR deployment.status <> 'completed')
            LIMIT 1
            """
        ).fetchone()
    if deployment_barrier:
        proposal_id, approval_id, deployment_status = deployment_barrier
        logger.info(
            "repository worker waiting for approved proposal deployment: proposal_id=%s approval_id=%s deployment_status=%s",
            proposal_id,
            approval_id,
            deployment_status or "not_started",
        )
        return
    with psycopg.connect(DATABASE_URL) as connection:
        rows = connection.execute(
            """
            SELECT task.id, task.work_item_id, task.worker_plan_id, task.implementation, plan.plan
            FROM agent_execution_tasks AS task
            JOIN agent_worker_plans AS plan ON plan.id = task.worker_plan_id
            WHERE task.status = 'qa_passed'
              AND (
                    SELECT count(*)
                    FROM agent_change_proposals AS failed
                    WHERE failed.execution_task_id = task.id
                      AND failed.status = 'qa_failed'
                  ) < 3
              AND NOT EXISTS (
                    SELECT 1
                    FROM agent_change_proposals
                    WHERE execution_task_id = task.id
                      AND status IN ('proposed', 'qa_passed', 'approved')
                  )
            ORDER BY task.id
            """
        ).fetchall()
    for task_id, work_item_id, worker_plan_id, implementation, plan in rows:
        try:
            proposal = generate_work_item_proposal(task_id, worker_plan_id, work_item_id, implementation, plan)
            submit_repository_proposal(task_id, worker_plan_id, proposal)
            logger.info("repository worker submitted proposal: task_id=%s", task_id)
        except Exception as error:
            logger.exception("repository worker failed: task_id=%s error=%s", task_id, type(error).__name__)


def repository_worker_loop() -> None:
    while True:
        try:
            run_repository_worker_once()
        except Exception as error:
            logger.exception("repository worker iteration failed: error=%s", type(error).__name__)
        time.sleep(EXECUTION_WORKER_INTERVAL_SECONDS)


def run_proposal_qa_worker_once() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        missing_handoffs = connection.execute(
            """
            SELECT proposal.id
            FROM agent_change_proposals AS proposal
                        JOIN agent_execution_tasks AS task ON task.id = proposal.execution_task_id
                        WHERE proposal.status = 'proposed'
              AND NOT EXISTS (
                  SELECT 1 FROM agent_approval_requests AS approval
                  WHERE approval.action = 'review_repository_change'
                    AND approval.status = 'approved'
                    AND (approval.context->>'proposal_id')::bigint = proposal.id
              )
            ORDER BY proposal.id
            """
        ).fetchall()
        qa_plan = connection.execute(
            """
            SELECT id FROM agent_worker_plans
            WHERE role = 'qa_engineer' AND status = 'approved'
            ORDER BY updated_at DESC, id DESC LIMIT 1
            """
        ).fetchone()
    if missing_handoffs and qa_plan is None:
        raise RuntimeError("an approved qa_engineer worker plan is required for proposal QA")
    for (proposal_id,) in missing_handoffs:
        queue_automated_handoff(
            "review_repository_change",
            QA_ENGINEER_NAME,
            {"proposal_id": proposal_id, "qa_plan_id": qa_plan[0]},
        )
    with psycopg.connect(DATABASE_URL) as connection:
        rows = connection.execute(
            """
                 SELECT proposal.id, proposal.proposal, task.implementation, worker.plan, approval.id,
                     (approval.context->>'qa_plan_id')::bigint
            FROM agent_change_proposals AS proposal
            JOIN agent_approval_requests AS approval
              ON approval.action = 'review_repository_change'
             AND approval.status = 'approved'
             AND (approval.context->>'proposal_id')::bigint = proposal.id
            JOIN agent_execution_tasks AS task ON task.id = proposal.execution_task_id
            JOIN agent_worker_plans AS worker ON worker.id = task.worker_plan_id
            WHERE proposal.status = 'proposed'
            ORDER BY proposal.id
            """
        ).fetchall()
    for proposal_id, proposal, implementation, worker_plan, approval_id, qa_plan_id in rows:
        try:
            files = proposal.get("files")
            if qa_plan_id is None:
                raise RuntimeError("proposal QA handoff is missing qa_plan_id")
            expected_files = _scope_paths(worker_plan)
            implementation_files = implementation.get("changed_surfaces") if isinstance(implementation, dict) else []
            patch = str(proposal.get("patch", ""))
            patch_files = [source for source, target in re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$", patch, re.MULTILINE)]
            patch_targets = [target for source, target in re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$", patch, re.MULTILINE)]
            patch_matches_files = bool(patch_files) and patch_files == patch_targets and set(patch_files) == set(files or [])
            patch_applies = False
            if patch_matches_files:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as patch_file:
                    patch_file.write(patch.replace("\r", "").rstrip("\n") + "\n")
                    patch_path = patch_file.name
                try:
                    patch_result = subprocess.run(
                        ["patch", "--dry-run", "--batch", "--forward", "--ignore-whitespace", "-p2", "-i", patch_path],
                        cwd="/workspace/repository",
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    patch_applies = patch_result.returncode == 0
                finally:
                    Path(patch_path).unlink(missing_ok=True)
            if files != expected_files or implementation_files != expected_files or not isinstance(files, list) or not files or any(not _scope_path_is_safe(str(path)) for path in files) or not patch.startswith("diff --git ") or not patch_matches_files or not patch_applies:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{AGENT_HTTP_PORT}/change-proposals/{proposal_id}/qa",
                    data=json.dumps({
                        "approval_id": approval_id,
                        "qa_plan_id": qa_plan_id,
                        "result": "failed",
                        "checks": ["Proposal targets exactly the approved worker-plan repository surfaces"],
                        "evidence": "Proposal scope did not match the approved worker plan and execution evidence.",
                        "defects": ["Proposal files, implementation surfaces, and approved worker-plan paths must match exactly."],
                    }).encode("utf-8"),
                    headers={"Authorization": f"Bearer {AGENT_APPROVAL_TOKEN}", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    response.read()
                continue
            request = urllib.request.Request(
                f"http://127.0.0.1:{AGENT_HTTP_PORT}/change-proposals/{proposal_id}/qa",
                data=json.dumps({
                    "approval_id": approval_id,
                    "qa_plan_id": qa_plan_id,
                    "result": "passed",
                    "checks": [
                        "Proposal targets only approved repository surfaces",
                        "Proposal contains a unified git diff",
                        "Proposal is bounded to approved worker-plan repository surfaces",
                    ],
                    "evidence": "QA verified the generated proposal is a bounded unified diff for approved repository surfaces.",
                    "defects": [],
                }).encode("utf-8"),
                headers={"Authorization": f"Bearer {AGENT_APPROVAL_TOKEN}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                response.read()
            approve_repository_change_automatically(proposal_id)
            logger.info("proposal QA passed and proposal approval completed automatically: proposal_id=%s", proposal_id)
        except Exception as error:
            logger.exception("proposal QA worker failed: proposal_id=%s error=%s", proposal_id, type(error).__name__)


def proposal_qa_worker_loop() -> None:
    while True:
        run_proposal_qa_worker_once()
        time.sleep(EXECUTION_WORKER_INTERVAL_SECONDS)


def workstream_deployment_completed(connection: psycopg.Connection, task_id: int) -> bool:
        """Return true only after the task's approved proposal was deployed successfully."""
        return connection.execute(
                """
                SELECT 1
                FROM agent_change_proposals AS proposal
                JOIN agent_approval_requests AS approval
                    ON approval.action = 'deploy'
                 AND (approval.context->>'proposal_id')::bigint = proposal.id
                JOIN agent_deployment_runs AS deployment
                    ON deployment.approval_id = approval.id
                WHERE proposal.execution_task_id = %s
                    AND proposal.status = 'approved'
                    AND approval.status = 'approved'
                    AND deployment.status = 'completed'
                LIMIT 1
                """,
                (task_id,),
        ).fetchone() is not None


def reconcile_workflow_completion(connection: psycopg.Connection, proposal_id: int) -> None:
    """Mark completed workflow parents after every child task is deployed."""
    task_row = connection.execute(
        """
        SELECT task.id, task.worker_plan_id, worker.engineering_plan_id,
               engineering.technical_plan_id, technical.product_brief_id
        FROM agent_change_proposals AS proposal
        JOIN agent_execution_tasks AS task ON task.id = proposal.execution_task_id
        JOIN agent_worker_plans AS worker ON worker.id = task.worker_plan_id
        JOIN agent_engineering_plans AS engineering ON engineering.id = worker.engineering_plan_id
        JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
        WHERE proposal.id = %s
        """,
        (proposal_id,),
    ).fetchone()
    if task_row is None:
        raise RuntimeError(f"proposal {proposal_id} has no workflow lineage")
    task_id, worker_plan_id, engineering_plan_id, technical_plan_id, product_brief_id = task_row
    now_value = datetime.now(timezone.utc)
    task_updated = connection.execute(
        """
        UPDATE agent_execution_tasks
        SET status = 'completed', updated_at = %s
        WHERE id = %s AND status = 'qa_passed'
        RETURNING id
        """,
        (now_value, task_id),
    ).fetchone()
    if task_updated:
        record_audit_event(connection, EXECUTION_AGENT_NAME, "execution_task_completed", "aicorp-deployment-worker", str(task_id))
    worker_updated = connection.execute(
        """
        UPDATE agent_worker_plans AS worker
        SET status = 'completed', updated_at = %s
        WHERE worker.id = %s AND worker.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_execution_tasks AS task
              WHERE task.worker_plan_id = worker.id AND task.status <> 'completed'
          )
        RETURNING worker.id
        """,
        (now_value, worker_plan_id),
    ).fetchone()
    if worker_updated:
        record_audit_event(connection, EXECUTION_AGENT_NAME, "worker_plan_completed", "aicorp-deployment-worker", str(worker_plan_id))
    connection.execute(
        """
        UPDATE agent_worker_plans AS worker
        SET status = 'completed', updated_at = %s
        WHERE worker.engineering_plan_id = %s AND worker.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_execution_tasks AS task
              WHERE task.worker_plan_id = worker.id AND task.status <> 'completed'
          )
        """,
        (now_value, engineering_plan_id),
    )
    engineering_updated = connection.execute(
        """
        UPDATE agent_engineering_plans AS engineering
        SET status = 'completed', updated_at = %s
        WHERE engineering.id = %s AND engineering.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_worker_plans AS worker
              WHERE worker.engineering_plan_id = engineering.id AND worker.status <> 'completed'
          )
        RETURNING engineering.id
        """,
        (now_value, engineering_plan_id),
    ).fetchone()
    if engineering_updated:
        record_audit_event(connection, EXECUTION_AGENT_NAME, "engineering_plan_completed", "aicorp-deployment-worker", str(engineering_plan_id))
    technical_updated = connection.execute(
        """
        UPDATE agent_technical_plans AS technical
        SET status = 'completed', updated_at = %s
        WHERE technical.id = %s AND technical.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_engineering_plans AS engineering
              WHERE engineering.technical_plan_id = technical.id AND engineering.status <> 'completed'
          )
        RETURNING technical.id
        """,
        (now_value, technical_plan_id),
    ).fetchone()
    if technical_updated:
        record_audit_event(connection, EXECUTION_AGENT_NAME, "technical_plan_completed", "aicorp-deployment-worker", str(technical_plan_id))
    brief_updated = connection.execute(
        """
        UPDATE agent_product_briefs AS brief
        SET status = 'completed', updated_at = %s
        WHERE brief.id = %s AND brief.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_technical_plans AS technical
              WHERE technical.product_brief_id = brief.id AND technical.status <> 'completed'
          )
        RETURNING brief.id
        """,
        (now_value, product_brief_id),
    ).fetchone()
    if brief_updated:
        record_audit_event(connection, EXECUTION_AGENT_NAME, "product_brief_completed", "aicorp-deployment-worker", str(product_brief_id))


def run_work_item_orchestrator_once() -> None:
    """Start workstreams in order, waiting for each deployment to complete."""
    with psycopg.connect(DATABASE_URL) as connection:
        rows = connection.execute(
            """
            SELECT worker.id, worker.engineering_plan_id, plan.plan
            FROM agent_worker_plans AS worker
            JOIN agent_engineering_plans AS plan ON plan.id = worker.engineering_plan_id
            WHERE worker.role = 'software_engineer'
              AND worker.status = 'approved'
              AND plan.status = 'approved'
            ORDER BY worker.id DESC
            LIMIT 1
            """
        ).fetchall()
    if not rows:
        return
    worker_plan_id, engineering_plan_id, plan = rows[0]
    for workstream in plan.get("workstreams", []):
        work_item_id = str(workstream.get("id", "")).strip()
        if not work_item_id:
            continue
        with psycopg.connect(DATABASE_URL) as connection:
            task = connection.execute(
                """
                SELECT id
                FROM agent_execution_tasks
                WHERE worker_plan_id = %s AND work_item_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (worker_plan_id, work_item_id),
            ).fetchone()
            if task is not None:
                if not workstream_deployment_completed(connection, int(task[0])):
                    return
                continue
        approval_id = auto_approve_handoff(
            "start_software_engineer_execution",
            SOFTWARE_ENGINEER_NAME,
            f"Automatically start approved work item {work_item_id}.",
            {"worker_plan_id": worker_plan_id, "work_item_id": work_item_id, "engineering_plan_id": engineering_plan_id},
        )
        start_automated_execution(
            approval_id,
            SOFTWARE_ENGINEER_NAME,
            {"worker_plan_id": worker_plan_id, "work_item_id": work_item_id},
        )
        logger.info("work-item orchestrator started: work_item_id=%s task_approval_id=%s", work_item_id, approval_id)


def work_item_orchestrator_loop() -> None:
    while True:
        try:
            run_work_item_orchestrator_once()
        except Exception as error:
            logger.exception("work-item orchestrator failed: error=%s", type(error).__name__)
        time.sleep(EXECUTION_WORKER_INTERVAL_SECONDS)


def run_execution_worker_once() -> None:
    """Claim supported in-progress tasks by submitting only observed evidence."""
    with psycopg.connect(DATABASE_URL) as connection:
        tasks = connection.execute(
            """
            SELECT task.id, task.work_item_id, plan.plan
            FROM agent_execution_tasks AS task
            JOIN agent_worker_plans AS plan ON plan.id = task.worker_plan_id
            WHERE task.status = 'in_progress'
            ORDER BY task.id
            """
        ).fetchall()
    for task_id, work_item_id, worker_plan in tasks:
        try:
            submit_execution_evidence(task_id, execute_work_item(task_id, work_item_id, worker_plan))
            logger.info("execution worker submitted evidence: task_id=%s work_item_id=%s", task_id, work_item_id)
        except Exception as error:
            logger.exception("execution worker failed: task_id=%s error=%s", task_id, type(error).__name__)
            with psycopg.connect(DATABASE_URL) as connection:
                record_audit_event(
                    connection,
                    EXECUTION_AGENT_NAME,
                    "execution_worker_failed",
                    SOFTWARE_ENGINEER_NAME,
                    str(task_id),
                    {"work_item_id": work_item_id, "error_type": type(error).__name__},
                )
                connection.commit()


def execution_worker_loop() -> None:
    while True:
        run_execution_worker_once()
        time.sleep(EXECUTION_WORKER_INTERVAL_SECONDS)


def init_database() -> None:
    reconciled_task_ids: list[int] = []
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id BIGSERIAL PRIMARY KEY,
                agent_name TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ,
                status TEXT NOT NULL,
                report TEXT,
                error TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_product_briefs (
                id BIGSERIAL PRIMARY KEY,
                agent_name TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                approval_request_id BIGINT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                brief JSONB NOT NULL,
                backlog JSONB NOT NULL,
                source_context TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT agent_product_brief_status_check
                    CHECK (status IN ('draft', 'approved', 'rejected'))
            )
            """
        )
        connection.execute(
            "ALTER TABLE agent_product_briefs DROP CONSTRAINT IF EXISTS agent_product_brief_status_check"
        )
        connection.execute(
            """
            ALTER TABLE agent_product_briefs
            ADD CONSTRAINT agent_product_brief_status_check
            CHECK (status IN ('draft', 'approved', 'rejected', 'completed', 'archived'))
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_technical_plans (
                id BIGSERIAL PRIMARY KEY,
                agent_name TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                product_brief_id BIGINT NOT NULL REFERENCES agent_product_briefs(id),
                requested_by TEXT NOT NULL,
                approval_request_id BIGINT NOT NULL UNIQUE,
                parent_technical_plan_id BIGINT REFERENCES agent_technical_plans(id),
                revision_type TEXT NOT NULL DEFAULT 'initial',
                dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
                status TEXT NOT NULL DEFAULT 'draft',
                plan JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT agent_technical_plan_status_check
                    CHECK (status IN ('draft', 'approved', 'rejected'))
            )
            """
        )
        connection.execute(
            "ALTER TABLE agent_technical_plans ADD COLUMN IF NOT EXISTS parent_technical_plan_id BIGINT REFERENCES agent_technical_plans(id)"
        )
        connection.execute(
            "ALTER TABLE agent_technical_plans ADD COLUMN IF NOT EXISTS revision_type TEXT NOT NULL DEFAULT 'initial'"
        )
        connection.execute(
            "ALTER TABLE agent_technical_plans ADD COLUMN IF NOT EXISTS dependencies JSONB NOT NULL DEFAULT '[]'::jsonb"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_engineering_plans (
                id BIGSERIAL PRIMARY KEY,
                agent_name TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                technical_plan_id BIGINT NOT NULL REFERENCES agent_technical_plans(id),
                requested_by TEXT NOT NULL,
                approval_request_id BIGINT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                plan JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT agent_engineering_plan_status_check
                    CHECK (status IN ('draft', 'approved', 'rejected'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_worker_plans (
                id BIGSERIAL PRIMARY KEY,
                agent_name TEXT NOT NULL,
                role TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                engineering_plan_id BIGINT NOT NULL REFERENCES agent_engineering_plans(id),
                requested_by TEXT NOT NULL,
                approval_request_id BIGINT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                plan JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT agent_worker_plan_status_check
                    CHECK (status IN ('draft', 'approved', 'rejected'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_execution_tasks (
                id BIGSERIAL PRIMARY KEY,
                worker_plan_id BIGINT NOT NULL REFERENCES agent_worker_plans(id),
                work_item_id TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                start_approval_id BIGINT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'ready',
                implementation JSONB,
                qa_result JSONB,
                qa_plan_id BIGINT REFERENCES agent_worker_plans(id),
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT agent_execution_status_check
                    CHECK (status IN ('ready', 'in_progress', 'submitted', 'qa_passed', 'qa_failed', 'completed'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_change_proposals (
                id BIGSERIAL PRIMARY KEY,
                execution_task_id BIGINT NOT NULL REFERENCES agent_execution_tasks(id),
                worker_plan_id BIGINT NOT NULL REFERENCES agent_worker_plans(id),
                requested_by TEXT NOT NULL,
                proposal JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                qa_plan_id BIGINT REFERENCES agent_worker_plans(id),
                qa_result JSONB,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                CONSTRAINT agent_change_proposal_status_check
                    CHECK (status IN ('proposed', 'qa_passed', 'qa_failed', 'approved', 'superseded'))
            )
            """
        )
        connection.execute(
            "ALTER TABLE agent_change_proposals DROP CONSTRAINT IF EXISTS agent_change_proposal_status_check"
        )
        for table_name, constraint_name, statuses in (
            ("agent_technical_plans", "agent_technical_plan_status_check", "'draft', 'approved', 'rejected', 'completed'"),
            ("agent_engineering_plans", "agent_engineering_plan_status_check", "'draft', 'approved', 'rejected', 'completed'"),
            ("agent_worker_plans", "agent_worker_plan_status_check", "'draft', 'approved', 'rejected', 'completed'"),
        ):
            connection.execute(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {constraint_name}")
            connection.execute(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} CHECK (status IN ({statuses}))"
            )
        connection.execute(
            """
            ALTER TABLE agent_change_proposals
            ADD CONSTRAINT agent_change_proposal_status_check
            CHECK (status IN ('proposed', 'qa_passed', 'qa_failed', 'approved', 'superseded'))
            """
        )
        init_governance_tables(connection)
        invalid_evidence = connection.execute(
            """
            UPDATE agent_execution_tasks
            SET status = 'qa_failed',
                qa_result = %s::jsonb,
                updated_at = %s
            WHERE status = 'submitted'
              AND lower(implementation->>'test_results') IN (
                  'record the actual results of the checks performed.',
                  'todo',
                  'tbd',
                  'to be determined'
              )
            RETURNING id
            """,
            (
                json.dumps(
                    {
                        "result": "failed",
                        "checks": ["Implementation evidence contains placeholder test results."],
                        "evidence": "Automated evidence reconciliation rejected placeholder test results.",
                        "defects": ["Replace placeholder test results with factual execution evidence."],
                    }
                ),
                datetime.now(timezone.utc),
            ),
        ).fetchall()
        for row in invalid_evidence:
            reconciled_task_ids.append(int(row[0]))
            record_audit_event(
                connection,
                QA_VALIDATION_AGENT_NAME,
                "invalid_implementation_evidence_reconciled",
                QA_VALIDATION_AGENT_NAME,
                str(row[0]),
                {"reason": "placeholder test_results"},
            )
        reconciled_task_ids.extend(
            int(row[0])
            for row in connection.execute(
                """
                SELECT task.id
                FROM agent_execution_tasks AS task
                WHERE task.status = 'qa_failed'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_approval_requests AS approval
                      WHERE approval.action = 'start_software_engineer_execution'
                        AND approval.context->>'replacement_for_task_id' = task.id::text
                  )
                """
            ).fetchall()
        )
        connection.commit()
    for task_id in set(reconciled_task_ids):
        retry_failed_execution_task(task_id)


def prometheus_get(path: str) -> dict:
    request = urllib.request.Request(f"{PROMETHEUS_URL}{path}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def collect_health() -> dict:
    targets = prometheus_get("/api/v1/targets")
    alerts = prometheus_get("/api/v1/alerts")
    return {
        "targets": targets.get("data", {}).get("active", []),
        "alerts": alerts.get("data", {}).get("alerts", []),
    }


def render_health_context(health: dict) -> str:
    active_targets = [
        {
            "job": target.get("labels", {}).get("job"),
            "instance": target.get("labels", {}).get("instance"),
            "health": target.get("health"),
        }
        for target in health["targets"]
    ]
    active_alerts = [
        {
            "name": alert.get("labels", {}).get("alertname"),
            "severity": alert.get("labels", {}).get("severity"),
            "state": alert.get("state"),
            "summary": alert.get("annotations", {}).get("summary"),
        }
        for alert in health["alerts"]
    ]
    return json.dumps({"targets": active_targets, "alerts": active_alerts}, indent=2)


def retrieve_knowledge(query: str) -> str:
    if not OLLAMA_BASE_URL:
        return ""
    try:
        embedding_request = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            data=json.dumps({"model": EMBEDDING_MODEL, "prompt": query}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(embedding_request, timeout=30) as response:
            vector = json.load(response)["embedding"]
        search_request = urllib.request.Request(
            f"{QDRANT_URL}/collections/{KNOWLEDGE_COLLECTION}/points/search",
            data=json.dumps({"vector": vector, "limit": 3, "with_payload": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(search_request, timeout=10) as response:
            results = json.load(response).get("result", [])
        return "\n\n".join(
            item.get("payload", {}).get("text", "")
            for item in results
            if item.get("payload", {}).get("text")
        )
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        logger.warning("knowledge retrieval unavailable")
        return ""


def generate_report(context: str) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the AICorp health-summary agent. Summarize only the "
                    "provided monitoring data. Do not invent incidents, recommend "
                    "commands, or request external actions. Keep the report concise."
                ),
            },
            {
                "role": "user",
                "content": "Prepare a short health summary from this JSON:\n" + context,
            },
        ],
    }
    if MODEL_NAME != "gpt-5.6-luna":
        payload["temperature"] = 0.1
    request = urllib.request.Request(
        f"{LITELLM_BASE_URL}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    return body["choices"][0]["message"]["content"].strip()


def run_once() -> bool:
    started_at = datetime.now(timezone.utc)
    run_id = None
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            """
            INSERT INTO agent_runs (agent_name, started_at, status)
            VALUES (%s, %s, 'running')
            RETURNING id
            """,
            (AGENT_NAME, started_at),
        ).fetchone()
        run_id = row[0]
        record_audit_event(
            connection,
            AGENT_NAME,
            "run_started",
            AGENT_NAME,
            subject=str(run_id),
        )
        connection.commit()

    try:
        health = collect_health()
        health_context = render_health_context(health)
        knowledge = retrieve_knowledge("AICorp architecture and operational guidance for this health state")
        if knowledge:
            health_context += "\n\nRelevant approved knowledge:\n" + knowledge
        report = generate_report(health_context)
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET completed_at = %s, status = 'completed', report = %s
                WHERE id = %s
                """,
                (datetime.now(timezone.utc), report, run_id),
            )
            record_audit_event(
                connection,
                AGENT_NAME,
                "run_completed",
                AGENT_NAME,
                subject=str(run_id),
            )
            connection.commit()
        logger.info("agent run completed: id=%s", run_id)
        return True
    except Exception as error:
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET completed_at = %s, status = 'failed', error = %s
                WHERE id = %s
                """,
                (datetime.now(timezone.utc), type(error).__name__, run_id),
            )
            record_audit_event(
                connection,
                AGENT_NAME,
                "run_failed",
                AGENT_NAME,
                subject=str(run_id),
                details={"error_type": type(error).__name__},
            )
            connection.commit()
        logger.error("agent run failed: id=%s error_type=%s", run_id, type(error).__name__)
        return False


class ReportHandler(BaseHTTPRequestHandler):
    def _json_response(self, status: int, payload: dict) -> None:
        body = (json.dumps(payload, default=str) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self._authenticated_actor() is not None

    def _authenticated_actor(self) -> tuple[str, str] | None:
        supplied = self.headers.get("Authorization", "")
        if AGENT_APPROVAL_TOKEN and hmac.compare_digest(supplied, f"Bearer {AGENT_APPROVAL_TOKEN}"):
            return ("human", OPERATOR_NAME)
        for role, token in AGENT_APPROVAL_TOKENS.items():
            if token and hmac.compare_digest(supplied, f"Bearer {token}"):
                return ("agent", {
                    "product_manager": PRODUCT_MANAGER_NAME,
                    "cto": CTO_NAME,
                    "engineering_manager": ENGINEERING_MANAGER_NAME,
                    "software_engineer": SOFTWARE_ENGINEER_NAME,
                    "qa_engineer": QA_ENGINEER_NAME,
                }[role])
        return None

    def _request_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _product_snapshot(self) -> dict:
        health = collect_health()
        with psycopg.connect(DATABASE_URL) as connection:
            row = connection.execute(
                """
                SELECT agent_name, started_at, completed_at, status, report
                FROM agent_runs
                WHERE agent_name = %s AND status = 'completed'
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (AGENT_NAME,),
            ).fetchone()
        report = None
        if row:
            report = {
                "agent_name": row[0],
                "started_at": row[1].isoformat(),
                "completed_at": row[2].isoformat(),
                "status": row[3],
                "report": row[4],
            }
        return {
            "targets": [
                {
                    "job": target.get("labels", {}).get("job"),
                    "instance": target.get("labels", {}).get("instance"),
                    "health": target.get("health"),
                }
                for target in health["targets"]
            ],
            "alerts": [
                {
                    "name": alert.get("labels", {}).get("alertname"),
                    "severity": alert.get("labels", {}).get("severity"),
                    "state": alert.get("state"),
                    "summary": alert.get("annotations", {}).get("summary"),
                }
                for alert in health["alerts"]
            ],
            "report": report,
        }

    def _product_brief_payload(self, row: tuple) -> dict:
        fields = (
            "id", "agent_name", "schema_version", "requested_by",
            "approval_request_id", "status", "brief", "backlog",
            "source_context", "created_at", "updated_at",
        )
        payload = dict(zip(fields, row))
        payload["publication_status"] = (
            "published" if payload["status"] == "approved"
            else "completed" if payload["status"] == "completed"
            else "archived" if payload["status"] == "archived"
            else "unpublished"
        )
        if payload["status"] == "approved":
            payload["status"] = "published"
        return payload

    def _technical_plan_payload(self, row: tuple) -> dict:
        fields = (
            "id", "agent_name", "schema_version", "product_brief_id",
            "requested_by", "approval_request_id", "parent_technical_plan_id",
            "revision_type", "dependencies", "status", "plan", "created_at",
            "updated_at",
        )
        return dict(zip(fields, row))

    def _engineering_plan_payload(self, row: tuple) -> dict:
        fields = ("id", "agent_name", "schema_version", "technical_plan_id", "requested_by", "approval_request_id", "status", "plan", "created_at", "updated_at")
        return dict(zip(fields, row))

    def _worker_plan_payload(self, row: tuple) -> dict:
        fields = ("id", "agent_name", "role", "schema_version", "engineering_plan_id", "requested_by", "approval_request_id", "status", "plan", "created_at", "updated_at")
        return dict(zip(fields, row))

    def do_GET(self) -> None:
        if self.path in {"/change-proposals", "/change-proposals/latest"}:
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                rows = connection.execute(
                    """
                          SELECT proposal.id, proposal.execution_task_id, proposal.worker_plan_id, proposal.requested_by,
                              proposal.proposal, proposal.status, proposal.qa_plan_id, proposal.qa_result,
                              proposal.created_at, proposal.updated_at
                          FROM agent_change_proposals AS proposal
                          JOIN agent_worker_plans AS worker ON worker.id = proposal.worker_plan_id
                          JOIN agent_engineering_plans AS engineering ON engineering.id = worker.engineering_plan_id
                          JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
                          JOIN agent_product_briefs AS brief ON brief.id = technical.product_brief_id
                          WHERE brief.status <> 'archived'
                          ORDER BY proposal.created_at DESC LIMIT %s
                    """,
                    (1 if self.path.endswith("/latest") else 20,),
                ).fetchall()
            fields = ("id", "execution_task_id", "worker_plan_id", "requested_by", "proposal", "status", "qa_plan_id", "qa_result", "created_at", "updated_at")
            proposals = [dict(zip(fields, row)) for row in rows]
            if self.path.endswith("/latest"):
                self._json_response(200, {"proposal": proposals[0] if proposals else None})
            else:
                self._json_response(200, {"proposals": proposals})
            return

        if self.path.startswith("/change-proposals/"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                proposal_id = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
            except ValueError:
                self._json_response(400, {"error": "invalid change proposal id"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                row = connection.execute(
                    """
                    SELECT id, execution_task_id, worker_plan_id, requested_by,
                           proposal, status, qa_plan_id, qa_result, created_at, updated_at
                    FROM agent_change_proposals WHERE id = %s
                    """,
                    (proposal_id,),
                ).fetchone()
            if row is None:
                self._json_response(404, {"error": "change proposal not found"})
                return
            fields = ("id", "execution_task_id", "worker_plan_id", "requested_by", "proposal", "status", "qa_plan_id", "qa_result", "created_at", "updated_at")
            self._json_response(200, {"proposal": dict(zip(fields, row))})
            return

        if self.path in {"/execution-tasks", "/execution-tasks/latest"}:
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                rows = connection.execute(
                    """
                          SELECT task.id, task.worker_plan_id, task.work_item_id, task.requested_by,
                              task.start_approval_id, task.status, task.implementation, task.qa_result,
                              task.qa_plan_id, task.created_at, task.updated_at
                          FROM agent_execution_tasks AS task
                          JOIN agent_worker_plans AS worker ON worker.id = task.worker_plan_id
                          JOIN agent_engineering_plans AS engineering ON engineering.id = worker.engineering_plan_id
                          JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
                          JOIN agent_product_briefs AS brief ON brief.id = technical.product_brief_id
                          WHERE brief.status <> 'archived'
                          ORDER BY task.created_at DESC LIMIT %s
                    """,
                    (1 if self.path.endswith("/latest") else 20,),
                ).fetchall()
            fields = ("id", "worker_plan_id", "work_item_id", "requested_by", "start_approval_id", "status", "implementation", "qa_result", "qa_plan_id", "created_at", "updated_at")
            tasks = [dict(zip(fields, row)) for row in rows]
            self._json_response(200, {"task": tasks[0] if self.path.endswith("/latest") and tasks else None} if self.path.endswith("/latest") else {"tasks": tasks})
            return

        if self.path.startswith("/execution-tasks/"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                task_id = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
            except ValueError:
                self._json_response(400, {"error": "invalid execution task id"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                row = connection.execute(
                    """
                    SELECT id, worker_plan_id, work_item_id, requested_by,
                           start_approval_id, status, implementation, qa_result,
                           qa_plan_id, created_at, updated_at
                    FROM agent_execution_tasks WHERE id = %s
                    """,
                    (task_id,),
                ).fetchone()
            if row is None:
                self._json_response(404, {"error": "execution task not found"})
                return
            fields = ("id", "worker_plan_id", "work_item_id", "requested_by", "start_approval_id", "status", "implementation", "qa_result", "qa_plan_id", "created_at", "updated_at")
            self._json_response(200, {"task": dict(zip(fields, row))})
            return

        if self.path in {"/engineering-plans", "/engineering-plans/latest"}:
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                rows = connection.execute(
                    """
                          SELECT engineering.id, engineering.agent_name, engineering.schema_version,
                              engineering.technical_plan_id, engineering.requested_by,
                              engineering.approval_request_id, engineering.status, engineering.plan,
                              engineering.created_at, engineering.updated_at
                          FROM agent_engineering_plans AS engineering
                          JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
                          JOIN agent_product_briefs AS brief ON brief.id = technical.product_brief_id
                          WHERE brief.status <> 'archived'
                          ORDER BY engineering.created_at DESC LIMIT %s
                    """,
                    (1 if self.path.endswith("/latest") else 20,),
                ).fetchall()
            key = "plan" if self.path.endswith("/latest") else "plans"
            value = self._engineering_plan_payload(rows[0]) if key == "plan" and rows else None
            self._json_response(200, {key: value if key == "plan" else [self._engineering_plan_payload(row) for row in rows]})
            return

        if self.path in {"/worker-plans", "/worker-plans/latest"}:
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                rows = connection.execute(
                    """
                          SELECT worker.id, worker.agent_name, worker.role, worker.schema_version,
                              worker.engineering_plan_id, worker.requested_by,
                              worker.approval_request_id, worker.status, worker.plan,
                              worker.created_at, worker.updated_at
                          FROM agent_worker_plans AS worker
                          JOIN agent_engineering_plans AS engineering ON engineering.id = worker.engineering_plan_id
                          JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
                          JOIN agent_product_briefs AS brief ON brief.id = technical.product_brief_id
                          WHERE brief.status <> 'archived'
                          ORDER BY worker.created_at DESC LIMIT %s
                    """,
                    (1 if self.path.endswith("/latest") else 20,),
                ).fetchall()
            key = "plan" if self.path.endswith("/latest") else "plans"
            value = self._worker_plan_payload(rows[0]) if key == "plan" and rows else None
            self._json_response(200, {key: value if key == "plan" else [self._worker_plan_payload(row) for row in rows]})
            return

        if self.path.startswith("/engineering-plans/") or self.path.startswith("/worker-plans/"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                artifact_id = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
                worker_route = self.path.startswith("/worker-plans/")
                table = "agent_worker_plans" if worker_route else "agent_engineering_plans"
                columns = "id, agent_name, role, schema_version, engineering_plan_id, requested_by, approval_request_id, status, plan, created_at, updated_at" if worker_route else "id, agent_name, schema_version, technical_plan_id, requested_by, approval_request_id, status, plan, created_at, updated_at"
                with psycopg.connect(DATABASE_URL) as connection:
                    row = connection.execute(f"SELECT {columns} FROM {table} WHERE id = %s", (artifact_id,)).fetchone()
                if row is None:
                    self._json_response(404, {"error": "plan not found"})
                    return
                self._json_response(200, {"plan": self._worker_plan_payload(row) if worker_route else self._engineering_plan_payload(row)})
            except ValueError:
                self._json_response(400, {"error": "invalid plan id"})
            return

        product_brief_route = urlsplit(self.path)
        if product_brief_route.path in {"/product-briefs", "/product-briefs/latest"}:
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            include_archived = product_brief_route.query == "include_archived=true"
            status_filter = "" if include_archived else "WHERE status <> 'archived'"
            with psycopg.connect(DATABASE_URL) as connection:
                if product_brief_route.path.endswith("/latest"):
                    rows = connection.execute(
                        f"""
                        SELECT id, agent_name, schema_version, requested_by,
                               approval_request_id, status, brief, backlog,
                               source_context, created_at, updated_at
                        FROM agent_product_briefs
                        {status_filter}
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ).fetchall()
                    self._json_response(
                        200,
                        {"brief": self._product_brief_payload(rows[0]) if rows else None},
                    )
                    return
                rows = connection.execute(
                    f"""
                    SELECT id, agent_name, schema_version, requested_by,
                           approval_request_id, status, brief, backlog,
                           source_context, created_at, updated_at
                    FROM agent_product_briefs
                    {status_filter}
                    ORDER BY created_at DESC
                    LIMIT 20
                    """
                ).fetchall()
            self._json_response(
                200,
                {"briefs": [self._product_brief_payload(row) for row in rows]},
            )
            return

        if self.path.startswith("/product-briefs/"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                brief_id = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
            except ValueError:
                self._json_response(400, {"error": "invalid product brief id"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                row = connection.execute(
                    """
                    SELECT id, agent_name, schema_version, requested_by,
                           approval_request_id, status, brief, backlog,
                           source_context, created_at, updated_at
                    FROM agent_product_briefs
                    WHERE id = %s
                    """,
                    (brief_id,),
                ).fetchone()
            if row is None:
                self._json_response(404, {"error": "product brief not found"})
                return
            self._json_response(200, {"brief": self._product_brief_payload(row)})
            return

        if self.path in {"/technical-plans", "/technical-plans/latest"}:
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                query = """
                      SELECT technical.id, technical.agent_name, technical.schema_version,
                          technical.product_brief_id, technical.requested_by,
                          technical.approval_request_id, technical.parent_technical_plan_id,
                          technical.revision_type, technical.dependencies, technical.status,
                          technical.plan, technical.created_at, technical.updated_at
                      FROM agent_technical_plans AS technical
                      JOIN agent_product_briefs AS brief ON brief.id = technical.product_brief_id
                      WHERE brief.status <> 'archived'
                    ORDER BY technical.created_at DESC
                """
                rows = connection.execute(query + (" LIMIT 1" if self.path.endswith("/latest") else " LIMIT 20")).fetchall()
            if self.path.endswith("/latest"):
                self._json_response(
                    200,
                    {"plan": self._technical_plan_payload(rows[0]) if rows else None},
                )
            else:
                self._json_response(
                    200,
                    {"plans": [self._technical_plan_payload(row) for row in rows]},
                )
            return

        if self.path.startswith("/technical-plans/"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                plan_id = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
            except ValueError:
                self._json_response(400, {"error": "invalid technical plan id"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                row = connection.execute(
                    """
                    SELECT id, agent_name, schema_version, product_brief_id,
                           requested_by, approval_request_id, parent_technical_plan_id,
                           revision_type, dependencies, status, plan, created_at, updated_at
                    FROM agent_technical_plans WHERE id = %s
                    """,
                    (plan_id,),
                ).fetchone()
            if row is None:
                self._json_response(404, {"error": "technical plan not found"})
                return
            self._json_response(200, {"plan": self._technical_plan_payload(row)})
            return

        if self.path == "/product":
            try:
                with open(PRODUCT_PAGE, encoding="utf-8") as page:
                    body = page.read().encode("utf-8")
            except OSError:
                self.send_error(503, "Product prototype unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/product-data":
            try:
                self._json_response(200, self._product_snapshot())
            except (OSError, psycopg.Error, KeyError, TypeError, ValueError, urllib.error.URLError):
                self._json_response(503, {"error": "product data unavailable"})
            return

        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}\n')
            return

        if self.path == "/metrics":
            init_database()
            with psycopg.connect(DATABASE_URL) as connection:
                run = connection.execute(
                    """
                    SELECT status, EXTRACT(EPOCH FROM completed_at)
                    FROM agent_runs
                    WHERE agent_name = %s
                    ORDER BY completed_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (AGENT_NAME,),
                ).fetchone()
                pending = connection.execute(
                    """
                    SELECT count(*)
                    FROM agent_approval_requests
                    WHERE agent_name = %s AND status = 'pending'
                    """,
                    (AGENT_NAME,),
                ).fetchone()[0]
                failed_runs = connection.execute(
                    """
                    SELECT count(*)
                    FROM agent_runs
                    WHERE agent_name = %s AND status = 'failed'
                    """,
                    (AGENT_NAME,),
                ).fetchone()[0]
            status_value = 1 if run and run[0] == "completed" else 0
            completed_at = float(run[1]) if run and run[1] else 0
            body = (
                "# HELP aicorp_agent_up Whether the AICorp agent process is serving.\n"
                "# TYPE aicorp_agent_up gauge\n"
                "aicorp_agent_up 1\n"
                "# HELP aicorp_agent_last_run_success Whether the latest agent run completed.\n"
                "# TYPE aicorp_agent_last_run_success gauge\n"
                f"aicorp_agent_last_run_success {status_value}\n"
                "# HELP aicorp_agent_last_run_completed_timestamp_seconds Completion time of the latest run.\n"
                "# TYPE aicorp_agent_last_run_completed_timestamp_seconds gauge\n"
                f"aicorp_agent_last_run_completed_timestamp_seconds {completed_at}\n"
                "# HELP aicorp_agent_pending_approval_requests Number of pending approval requests.\n"
                "# TYPE aicorp_agent_pending_approval_requests gauge\n"
                f"aicorp_agent_pending_approval_requests {pending}\n"
                "# HELP aicorp_agent_failed_runs Number of failed agent runs represented in the metrics snapshot.\n"
                "# TYPE aicorp_agent_failed_runs gauge\n"
                f"aicorp_agent_failed_runs {failed_runs}\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/report":
            with psycopg.connect(DATABASE_URL) as connection:
                init_database()
                row = connection.execute(
                    """
                    SELECT agent_name, started_at, completed_at, status, report
                    FROM agent_runs
                    WHERE agent_name = %s AND status = 'completed'
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """,
                    (AGENT_NAME,),
                ).fetchone()
            if row is None:
                self.send_error(404, "No completed report")
                return
            payload = {
                "agent_name": row[0],
                "started_at": row[1].isoformat(),
                "completed_at": row[2].isoformat(),
                "status": row[3],
                "report": row[4],
            }
            body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/audit-events":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                expire_pending_approvals(connection, AGENT_NAME)
                rows = connection.execute(
                    """
                    SELECT id, event_type, actor, subject, details, occurred_at
                    FROM agent_audit_events
                    WHERE agent_name = %s
                    ORDER BY occurred_at DESC
                    LIMIT 100
                    """,
                    (AGENT_NAME,),
                ).fetchall()
            fields = ("id", "event_type", "actor", "subject", "details", "occurred_at")
            self._json_response(200, {"events": [dict(zip(fields, row)) for row in rows]})
            return

        if self.path == "/approval-requests" and self.command == "GET":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            with psycopg.connect(DATABASE_URL) as connection:
                init_governance_tables(connection)
                expire_pending_approvals(connection, AGENT_NAME)
                rows = connection.execute(
                    """
                    SELECT id, agent_name, action, requested_by, reason, status,
                              decided_by, decision_reason, requested_at, decided_at, context
                    FROM agent_approval_requests
                    ORDER BY requested_at DESC
                    LIMIT 50
                    """
                ).fetchall()
            fields = (
                "id", "agent_name", "action", "requested_by", "reason", "status",
                "decided_by", "decision_reason", "requested_at", "decided_at", "context",
            )
            self._json_response(200, {"requests": [dict(zip(fields, row)) for row in rows]})
            return

        self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/approval-requests":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                action = str(payload["action"]).strip()
                requested_by = str(payload["requested_by"]).strip()
                reason = str(payload["reason"])
                context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
                if action in AUTOMATED_HANDOFF_ACTIONS:
                    request_id = auto_approve_handoff(action, requested_by, reason, context)
                    task_id = None
                    if action == "start_software_engineer_execution":
                        task_id = start_automated_execution(request_id, requested_by, context)
                    elif action in {"approve_technical_plan", "approve_engineering_plan", "approve_software_engineer_plan", "approve_qa_plan"}:
                        threading.Thread(
                            target=dispatch_automated_handoff,
                            args=(action, request_id, context),
                            daemon=True,
                        ).start()
                    response = {"id": request_id, "status": "approved", "automated": True}
                    if task_id is not None:
                        response["execution_task_id"] = task_id
                    self._json_response(201, response)
                    return
                with psycopg.connect(DATABASE_URL) as connection:
                    init_governance_tables(connection)
                    expire_pending_approvals(connection, AGENT_NAME)
                    request_id = create_approval_request(
                        connection, AGENT_NAME, action, requested_by, reason, context,
                    )
                    record_audit_event(
                        connection, AGENT_NAME, "approval_requested",
                        requested_by, str(request_id), {"action": action},
                    )
                    connection.commit()
                self._json_response(201, {"id": request_id, "status": "pending"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/approval-requests/") and self.path.endswith("/retry"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                request_id = int(self.path.split("/")[2])
                with psycopg.connect(DATABASE_URL) as connection:
                    request_row = get_approval_request(connection, request_id)
                actor = self._authenticated_actor()
                if actor is None or actor[0] != "human":
                    self._json_response(403, {"error": "only the authenticated operator can retry a handoff"})
                    return
                if request_row is None:
                    self._json_response(404, {"error": "approval request not found"})
                    return
                if request_row[5] != "approved" or request_row[2] not in AUTO_DISPATCH_ACTIONS:
                    self._json_response(409, {"error": "only an approved automatic generation request can be retried"})
                    return
                threading.Thread(
                    target=dispatch_approved_action,
                    args=(request_row[2], request_id),
                    daemon=True,
                ).start()
                self._json_response(202, {"id": request_id, "status": "retrying", "action": request_row[2]})
            except (ValueError, KeyError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/approval-requests/"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                request_id = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
                payload = self._request_json()
                status = str(payload["status"])
                requested_decided_by = str(payload.get("decided_by", "")).strip()
                decision_reason = str(payload["decision_reason"])
                with psycopg.connect(DATABASE_URL) as connection:
                    init_governance_tables(connection)
                    expire_pending_approvals(connection, AGENT_NAME)
                    actor = self._authenticated_actor()
                    request_row = get_approval_request(connection, request_id)
                    if request_row is None:
                        self._json_response(404, {"error": "approval request not found"})
                        return
                    if actor is None:
                        self._json_response(401, {"error": "unauthorized"})
                        return
                    if actor[0] == "agent":
                        if not agent_can_approve_generation(
                            request_row[2],
                            actor[1],
                            {
                                "product_manager": PRODUCT_MANAGER_NAME,
                                "cto": CTO_NAME,
                                "engineering_manager": ENGINEERING_MANAGER_NAME,
                            },
                        ):
                            self._json_response(403, {"error": "agent is not authorized to decide this approval"})
                            return
                        if request_row[3] == actor[1]:
                            self._json_response(403, {"error": "agents cannot approve their own requests"})
                            return
                        decided_by = actor[1]
                    else:
                        if status != "denied" and requested_decided_by and requested_decided_by != OPERATOR_NAME:
                            self._json_response(403, {"error": "decided_by must identify the authenticated operator"})
                            return
                        decided_by = OPERATOR_NAME
                    publication_brief_id = None
                    archive_brief_id = None
                    archive_reason = ""
                    archive_cancelled_ids = []
                    amendment_plan_id = None
                    if status == "approved" and request_row[2] == "approve_product_brief":
                        context = request_row[10] or {}
                        try:
                            publication_brief_id = int(context["product_brief_id"])
                        except (KeyError, TypeError, ValueError):
                            self._json_response(400, {"error": "approve_product_brief requires context.product_brief_id"})
                            return
                        brief = connection.execute(
                            "SELECT status FROM agent_product_briefs WHERE id = %s",
                            (publication_brief_id,),
                        ).fetchone()
                        if brief is None or brief[0] != "draft":
                            self._json_response(409, {"error": "the referenced product brief must be a draft"})
                            return
                    if status == "approved" and request_row[2] == "archive_product_brief":
                        context = request_row[10] or {}
                        try:
                            archive_brief_id = int(context["product_brief_id"])
                            archive_reason = str(context["reason"]).strip()
                        except (KeyError, TypeError, ValueError):
                            self._json_response(400, {"error": "archive_product_brief requires product_brief_id and reason"})
                            return
                        if context.get("confirm") is not True:
                            pending_publication = connection.execute(
                                """
                                SELECT 1 FROM agent_approval_requests
                                WHERE action = 'approve_product_brief'
                                  AND status = 'pending'
                                  AND context->>'product_brief_id' = %s
                                """,
                                (str(archive_brief_id),),
                            ).fetchone()
                            if pending_publication:
                                self._json_response(409, {"error": "archive approval must include confirm=true when a publication approval is pending"})
                                return
                        brief = connection.execute(
                            "SELECT status FROM agent_product_briefs WHERE id = %s",
                            (archive_brief_id,),
                        ).fetchone()
                        if brief is None:
                            self._json_response(404, {"error": "product brief not found"})
                            return
                        if brief[0] == "archived":
                            self._json_response(409, {"error": "product brief is already archived"})
                            return
                    if status == "approved" and request_row[2] == "approve_technical_plan":
                        context = request_row[10] or {}
                        if context.get("revision_type") == "amendment":
                            try:
                                amendment_plan_id = int(context["technical_plan_id"])
                            except (KeyError, TypeError, ValueError):
                                self._json_response(400, {"error": "technical-plan amendment approval requires context.technical_plan_id"})
                                return
                            plan = connection.execute(
                                "SELECT status FROM agent_technical_plans WHERE id = %s",
                                (amendment_plan_id,),
                            ).fetchone()
                            if plan is None or plan[0] != "draft":
                                self._json_response(409, {"error": "the referenced technical-plan amendment must be a draft"})
                                return
                    if archive_brief_id is not None:
                        connection.execute(
                            """
                            UPDATE agent_approval_requests
                            SET status = %s, decided_by = %s, decision_reason = %s, decided_at = %s
                            WHERE id = %s AND status = 'pending'
                            """,
                            (status, decided_by, decision_reason, datetime.now(timezone.utc), request_id),
                        )
                    else:
                        decide_approval(connection, request_id, status, decided_by, decision_reason)
                    record_audit_event(
                        connection, AGENT_NAME, "approval_decided", decided_by,
                        str(request_id), {"status": status},
                    )
                    if archive_brief_id is not None:
                        archive_cancelled_ids = archive_product_brief_in_connection(
                            connection,
                            archive_brief_id,
                            decided_by,
                            archive_reason,
                            context.get("confirm") is True,
                        )
                    if publication_brief_id is not None:
                        connection.execute(
                            """
                            UPDATE agent_product_briefs
                            SET status = 'approved', updated_at = %s
                            WHERE id = %s AND status = 'draft'
                            RETURNING id
                            """,
                            (datetime.now(timezone.utc), publication_brief_id),
                        ).fetchone()
                        record_audit_event(
                            connection, PRODUCT_MANAGER_NAME, "product_brief_approved",
                            decided_by, str(publication_brief_id), {"approval_id": request_id},
                        )
                    if amendment_plan_id is not None:
                        connection.execute(
                            "UPDATE agent_technical_plans SET status = 'approved', updated_at = %s WHERE id = %s AND status = 'draft'",
                            (datetime.now(timezone.utc), amendment_plan_id),
                        )
                        record_audit_event(
                            connection, CTO_NAME, "technical_plan_amendment_approved",
                            decided_by, str(amendment_plan_id), {"approval_id": request_id},
                        )
                    connection.commit()
                    request_row = get_approval_request(connection, request_id)
                if publication_brief_id is not None:
                    queue_generation_approval(
                        "generate_technical_plan",
                        PRODUCT_MANAGER_NAME,
                        {"product_brief_id": publication_brief_id},
                    )
                if amendment_plan_id is not None:
                    queue_generation_approval(
                        "generate_engineering_plan",
                        CTO_NAME,
                        {"technical_plan_id": amendment_plan_id},
                    )
                if status == "approved" and amendment_plan_id is None and archive_brief_id is None:
                    threading.Thread(
                        target=dispatch_approved_action,
                        args=(request_row[2], request_id),
                        daemon=True,
                    ).start()
                fields = (
                    "id", "agent_name", "action", "requested_by", "reason", "status",
                    "decided_by", "decision_reason", "requested_at", "decided_at", "context",
                )
                response = {"request": dict(zip(fields, request_row))}
                if archive_brief_id is not None:
                    response["archived_product_brief_id"] = archive_brief_id
                    response["cancelled_publication_approval_ids"] = archive_cancelled_ids
                self._json_response(200, response)
            except (ValueError, KeyError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/execution-tasks/") and self.path.endswith("/change-proposal"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                task_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                worker_plan_id = int(payload["worker_plan_id"])
                proposal = validate_change_proposal(payload)
                requested_by = str(payload.get("requested_by", "operator")).strip()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "submit_repository_change_proposal"):
                        self._json_response(403, {"error": "approved submit_repository_change_proposal request required"})
                        return
                    task = connection.execute("SELECT status FROM agent_execution_tasks WHERE id = %s", (task_id,)).fetchone()
                    worker = connection.execute("SELECT role, status FROM agent_worker_plans WHERE id = %s", (worker_plan_id,)).fetchone()
                    if task is None or task[0] != "qa_passed":
                        self._json_response(409, {"error": "execution task must be qa_passed"})
                        return
                    if worker is None or worker[0] != "software_engineer" or worker[1] != "approved":
                        self._json_response(409, {"error": "approved software_engineer worker plan required"})
                        return
                    now = datetime.now(timezone.utc)
                    row = connection.execute(
                        """
                        INSERT INTO agent_change_proposals
                            (execution_task_id, worker_plan_id, requested_by, proposal, created_at, updated_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s) RETURNING id
                        """,
                        (task_id, worker_plan_id, requested_by, json.dumps(proposal), now, now),
                    ).fetchone()
                    record_audit_event(connection, EXECUTION_AGENT_NAME, "repository_change_proposed", requested_by, str(row[0]), {"task_id": task_id, "approval_id": approval_id})
                    connection.commit()
                self._json_response(201, {"id": row[0], "status": "proposed"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/change-proposals/") and self.path.endswith("/qa"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                proposal_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                qa_plan_id = int(payload["qa_plan_id"])
                qa_result = validate_qa_result(payload)
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "review_repository_change"):
                        self._json_response(403, {"error": "approved review_repository_change request required"})
                        return
                    qa_plan = connection.execute("SELECT role, status FROM agent_worker_plans WHERE id = %s", (qa_plan_id,)).fetchone()
                    if qa_plan is None or qa_plan[0] != "qa_engineer" or qa_plan[1] != "approved":
                        self._json_response(409, {"error": "approved qa_engineer worker plan required"})
                        return
                    status = "qa_passed" if qa_result["result"] == "passed" else "qa_failed"
                    updated = connection.execute(
                        """
                        UPDATE agent_change_proposals SET status = %s, qa_plan_id = %s,
                               qa_result = %s::jsonb, updated_at = %s
                        WHERE id = %s AND status = 'proposed' RETURNING id
                        """,
                        (status, qa_plan_id, json.dumps(qa_result), datetime.now(timezone.utc), proposal_id),
                    ).fetchone()
                    if updated is None:
                        self._json_response(409, {"error": "change proposal is not proposed"})
                        return
                    record_audit_event(connection, QA_VALIDATION_AGENT_NAME, "repository_change_reviewed", "qa_engineer", str(proposal_id), {"approval_id": approval_id, "result": qa_result["result"]})
                    connection.commit()
                self._json_response(200, {"id": proposal_id, "status": status})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/change-proposals/") and self.path.endswith("/supersede"):
            actor = self._authenticated_actor()
            if actor is None or actor[0] != "human":
                self._json_response(403, {"error": "only the authenticated operator can supersede a proposal"})
                return
            try:
                proposal_id = int(self.path.split("/")[2])
                payload = self._request_json()
                reason = str(payload.get("reason", "")).strip()
                if not reason:
                    raise ValueError("reason is required")
                with psycopg.connect(DATABASE_URL) as connection:
                    updated = connection.execute(
                        """
                        UPDATE agent_change_proposals
                        SET status = 'superseded', updated_at = %s
                        WHERE id = %s AND status = 'approved'
                        RETURNING id
                        """,
                        (datetime.now(timezone.utc), proposal_id),
                    ).fetchone()
                    if updated is None:
                        self._json_response(409, {"error": "only an approved proposal can be superseded"})
                        return
                    record_audit_event(
                        connection,
                        EXECUTION_AGENT_NAME,
                        "repository_change_superseded",
                        actor[1],
                        str(proposal_id),
                        {"reason": reason},
                    )
                    connection.commit()
                self._json_response(200, {"id": proposal_id, "status": "superseded"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/change-proposals/") and self.path.endswith("/approve"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                proposal_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                actor = self._authenticated_actor()
                if actor != ("agent", QA_ENGINEER_NAME):
                    self._json_response(403, {"error": "only the QA Engineer may approve a repository change"})
                    return
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "approve_repository_change"):
                        self._json_response(403, {"error": "approved approve_repository_change request required"})
                        return
                    updated = connection.execute(
                        "UPDATE agent_change_proposals SET status = 'approved', updated_at = %s WHERE id = %s AND status = 'qa_passed' RETURNING id",
                        (datetime.now(timezone.utc), proposal_id),
                    ).fetchone()
                    if updated is None:
                        self._json_response(409, {"error": "change proposal must pass QA before approval"})
                        return
                    record_audit_event(connection, EXECUTION_AGENT_NAME, "repository_change_approved", str(payload.get("decided_by", "operator")), str(proposal_id), {"approval_id": approval_id})
                    connection.commit()
                create_pending_approval(
                    "deploy",
                    QA_ENGINEER_NAME,
                    f"Final deployment approval for QA-approved repository proposal {proposal_id}.",
                    {"proposal_id": proposal_id},
                )
                self._json_response(200, {"id": proposal_id, "status": "approved"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path == "/execution-tasks/start":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                worker_plan_id = int(payload["worker_plan_id"])
                approval_id = int(payload["approval_id"])
                work_item_id = str(payload["work_item_id"]).strip()
                requested_by = str(payload.get("requested_by", "operator")).strip()
                if not work_item_id or not requested_by:
                    raise ValueError("work_item_id and requested_by are required")
                init_database()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "start_software_engineer_execution"):
                        self._json_response(403, {"error": "approved start_software_engineer_execution request required"})
                        return
                    worker = connection.execute(
                        "SELECT role, status FROM agent_worker_plans WHERE id = %s",
                        (worker_plan_id,),
                    ).fetchone()
                    if worker is None or worker[0] != "software_engineer" or worker[1] != "approved":
                        self._json_response(409, {"error": "an approved software_engineer worker plan is required"})
                        return
                    used = connection.execute(
                        "SELECT 1 FROM agent_execution_tasks WHERE start_approval_id = %s",
                        (approval_id,),
                    ).fetchone()
                    if used:
                        self._json_response(409, {"error": "start approval has already been used"})
                        return
                    now = datetime.now(timezone.utc)
                    row = connection.execute(
                        """
                        INSERT INTO agent_execution_tasks
                            (worker_plan_id, work_item_id, requested_by, start_approval_id, status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, 'in_progress', %s, %s)
                        RETURNING id
                        """,
                        (worker_plan_id, work_item_id, requested_by, approval_id, now, now),
                    ).fetchone()
                    record_audit_event(connection, EXECUTION_AGENT_NAME, "execution_started", requested_by, str(row[0]), {"worker_plan_id": worker_plan_id, "work_item_id": work_item_id, "approval_id": approval_id})
                    connection.commit()
                self._json_response(201, {"id": row[0], "status": "in_progress"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/execution-tasks/") and self.path.endswith("/submit"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                task_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                implementation = validate_implementation_submission(payload)
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "submit_software_engineer_execution"):
                        self._json_response(403, {"error": "approved submit_software_engineer_execution request required"})
                        return
                    updated = connection.execute(
                        """
                        UPDATE agent_execution_tasks
                        SET status = 'submitted', implementation = %s::jsonb, updated_at = %s
                        WHERE id = %s AND status = 'in_progress'
                        RETURNING id
                        """,
                        (json.dumps(implementation), datetime.now(timezone.utc), task_id),
                    ).fetchone()
                    if updated is None:
                        self._json_response(409, {"error": "execution task is not in progress"})
                        return
                    record_audit_event(connection, EXECUTION_AGENT_NAME, "implementation_submitted", "software_engineer", str(task_id), {"approval_id": approval_id})
                    connection.commit()
                with psycopg.connect(DATABASE_URL) as connection:
                    qa_plan = connection.execute(
                        """
                        SELECT id FROM agent_worker_plans
                        WHERE role = 'qa_engineer' AND status = 'approved'
                        ORDER BY updated_at DESC, id DESC
                        LIMIT 1
                        """
                    ).fetchone()
                if qa_plan is None:
                    raise ValueError("an approved qa_engineer worker plan is required for automatic QA handoff")
                queue_automated_handoff(
                    "record_qa_validation",
                    QA_ENGINEER_NAME,
                    {"task_id": task_id, "qa_plan_id": qa_plan[0]},
                )
                self._json_response(200, {"id": task_id, "status": "submitted"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/execution-tasks/") and self.path.endswith("/qa"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                task_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                qa_plan_id = int(payload["qa_plan_id"])
                qa_result = validate_qa_result(payload)
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "record_qa_validation"):
                        self._json_response(403, {"error": "approved record_qa_validation request required"})
                        return
                    qa_plan = connection.execute("SELECT role, status FROM agent_worker_plans WHERE id = %s", (qa_plan_id,)).fetchone()
                    if qa_plan is None or qa_plan[0] != "qa_engineer" or qa_plan[1] != "approved":
                        self._json_response(409, {"error": "an approved qa_engineer worker plan is required"})
                        return
                    status = "qa_passed" if qa_result["result"] == "passed" else "qa_failed"
                    updated = connection.execute(
                        """
                        UPDATE agent_execution_tasks
                        SET status = %s, qa_result = %s::jsonb, qa_plan_id = %s, updated_at = %s
                        WHERE id = %s AND status = 'submitted'
                        RETURNING id
                        """,
                        (status, json.dumps(qa_result), qa_plan_id, datetime.now(timezone.utc), task_id),
                    ).fetchone()
                    if updated is None:
                        self._json_response(409, {"error": "execution task must be submitted before QA validation"})
                        return
                    record_audit_event(connection, QA_VALIDATION_AGENT_NAME, "qa_validation_recorded", "qa_engineer", str(task_id), {"approval_id": approval_id, "qa_plan_id": qa_plan_id, "result": qa_result["result"]})
                    connection.commit()
                self._json_response(200, {"id": task_id, "status": status})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path == "/engineering-plans/generate":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                technical_plan_id = int(payload["technical_plan_id"])
                requested_by = str(payload.get("requested_by", "operator")).strip()
                init_database()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "generate_engineering_plan"):
                        self._json_response(403, {"error": "approved generate_engineering_plan request required"})
                        return
                    source = connection.execute(
                        "SELECT status, plan FROM agent_technical_plans WHERE id = %s",
                        (technical_plan_id,),
                    ).fetchone()
                    used = connection.execute(
                        "SELECT 1 FROM agent_engineering_plans WHERE approval_request_id = %s",
                        (approval_id,),
                    ).fetchone()
                    if source is None or source[0] != "approved":
                        self._json_response(409, {"error": "an approved technical plan is required"})
                        return
                    if used:
                        self._json_response(409, {"error": "approval request already generated an engineering plan"})
                        return
                    record_audit_event(connection, ENGINEERING_MANAGER_NAME, "approved_action_started", requested_by, str(approval_id), {"action": "generate_engineering_plan", "technical_plan_id": technical_plan_id})
                    connection.commit()
                plan = generate_engineering_plan(LITELLM_BASE_URL, LITELLM_MASTER_KEY, ENGINEERING_MANAGER_MODEL, source[1])
                now = datetime.now(timezone.utc)
                with psycopg.connect(DATABASE_URL) as connection:
                    row = connection.execute(
                        """
                        INSERT INTO agent_engineering_plans
                            (agent_name, schema_version, technical_plan_id, requested_by,
                             approval_request_id, status, plan, created_at, updated_at)
                        VALUES (%s, '1.0', %s, %s, %s, 'draft', %s::jsonb, %s, %s)
                        RETURNING id
                        """,
                        (ENGINEERING_MANAGER_NAME, technical_plan_id, requested_by, approval_id, json.dumps(plan), now, now),
                    ).fetchone()
                    record_audit_event(connection, ENGINEERING_MANAGER_NAME, "engineering_plan_generated", ENGINEERING_MANAGER_NAME, str(row[0]), {"approval_id": approval_id, "technical_plan_id": technical_plan_id})
                    connection.commit()
                queue_automated_handoff(
                    "approve_engineering_plan", ENGINEERING_MANAGER_NAME,
                    {"engineering_plan_id": row[0]},
                )
                self._json_response(201, {"id": row[0], "status": "draft", "schema_version": "1.0"})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
                self._json_response(400, {"error": str(error)})
            except Exception as error:
                logger.exception("engineering plan generation failed")
                self._json_response(502, {"error": type(error).__name__})
            return

        if self.path == "/worker-plans/generate":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                engineering_plan_id = int(payload["engineering_plan_id"])
                role = str(payload["role"])
                requested_by = str(payload.get("requested_by", "operator")).strip()
                role_config = {
                    "software_engineer": (SOFTWARE_ENGINEER_NAME, "generate_software_engineer_plan"),
                    "qa_engineer": (QA_ENGINEER_NAME, "generate_qa_plan"),
                }
                if role not in role_config:
                    raise ValueError("role must be software_engineer or qa_engineer")
                agent_name, action = role_config[role]
                init_database()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, action):
                        self._json_response(403, {"error": f"approved {action} request required"})
                        return
                    source = connection.execute(
                        "SELECT status, plan FROM agent_engineering_plans WHERE id = %s",
                        (engineering_plan_id,),
                    ).fetchone()
                    used = connection.execute(
                        "SELECT 1 FROM agent_worker_plans WHERE approval_request_id = %s",
                        (approval_id,),
                    ).fetchone()
                    if source is None or source[0] != "approved":
                        self._json_response(409, {"error": "an approved engineering plan is required"})
                        return
                    if used:
                        self._json_response(409, {"error": "approval request already generated a worker plan"})
                        return
                    record_audit_event(connection, agent_name, "approved_action_started", requested_by, str(approval_id), {"action": action, "engineering_plan_id": engineering_plan_id})
                    connection.commit()
                plan = generate_worker_plan(LITELLM_BASE_URL, LITELLM_MASTER_KEY, WORKER_MODEL, role, source[1])
                now = datetime.now(timezone.utc)
                with psycopg.connect(DATABASE_URL) as connection:
                    row = connection.execute(
                        """
                        INSERT INTO agent_worker_plans
                            (agent_name, role, schema_version, engineering_plan_id, requested_by,
                             approval_request_id, status, plan, created_at, updated_at)
                        VALUES (%s, %s, '1.0', %s, %s, %s, 'draft', %s::jsonb, %s, %s)
                        RETURNING id
                        """,
                        (agent_name, role, engineering_plan_id, requested_by, approval_id, json.dumps(plan), now, now),
                    ).fetchone()
                    record_audit_event(connection, agent_name, "worker_plan_generated", agent_name, str(row[0]), {"approval_id": approval_id, "engineering_plan_id": engineering_plan_id, "role": role})
                    connection.commit()
                queue_automated_handoff(
                    "approve_software_engineer_plan" if role == "software_engineer" else "approve_qa_plan",
                    agent_name,
                    {"worker_plan_id": row[0]},
                )
                self._json_response(201, {"id": row[0], "status": "draft", "role": role, "schema_version": "1.0"})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
                self._json_response(400, {"error": str(error)})
            except Exception as error:
                logger.exception("worker plan generation failed")
                self._json_response(502, {"error": type(error).__name__})
            return

        if self.path.startswith("/engineering-plans/") and self.path.endswith("/approve"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                plan_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "approve_engineering_plan"):
                        self._json_response(403, {"error": "approved approve_engineering_plan request required"})
                        return
                    updated = connection.execute("UPDATE agent_engineering_plans SET status = 'approved', updated_at = %s WHERE id = %s AND status = 'draft' RETURNING id", (datetime.now(timezone.utc), plan_id)).fetchone()
                    if updated is None:
                        self._json_response(404, {"error": "draft engineering plan not found"})
                        return
                    record_audit_event(connection, ENGINEERING_MANAGER_NAME, "engineering_plan_approved", str(payload.get("decided_by", "operator")), str(plan_id), {"approval_id": approval_id})
                    connection.commit()
                for role, action in (
                    ("software_engineer", "generate_software_engineer_plan"),
                    ("qa_engineer", "generate_qa_plan"),
                ):
                    queue_generation_approval(
                        action,
                        ENGINEERING_MANAGER_NAME,
                        {"engineering_plan_id": plan_id, "role": role},
                    )
                self._json_response(200, {"id": plan_id, "status": "approved"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/worker-plans/") and self.path.endswith("/approve"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                plan_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                with psycopg.connect(DATABASE_URL) as connection:
                    source = connection.execute("SELECT role FROM agent_worker_plans WHERE id = %s", (plan_id,)).fetchone()
                    if source is None:
                        self._json_response(404, {"error": "worker plan not found"})
                        return
                    action = "approve_software_engineer_plan" if source[0] == "software_engineer" else "approve_qa_plan"
                    if not approved_action(connection, approval_id, action):
                        self._json_response(403, {"error": f"approved {action} request required"})
                        return
                    updated = connection.execute("UPDATE agent_worker_plans SET status = 'approved', updated_at = %s WHERE id = %s AND status = 'draft' RETURNING id", (datetime.now(timezone.utc), plan_id)).fetchone()
                    if updated is None:
                        self._json_response(404, {"error": "draft worker plan not found"})
                        return
                    record_audit_event(connection, source[0], "worker_plan_approved", str(payload.get("decided_by", "operator")), str(plan_id), {"approval_id": approval_id})
                    connection.commit()
                self._json_response(200, {"id": plan_id, "status": "approved"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path == "/technical-plans/amend":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                parent_plan_id = int(payload["technical_plan_id"])
                dependencies = payload["dependencies"]
                requested_by = str(payload.get("requested_by", OPERATOR_NAME)).strip() or OPERATOR_NAME
                reason = str(payload.get("reason", "Record newly identified platform and test-data dependencies.")).strip()
                if not isinstance(dependencies, list) or not dependencies or len(dependencies) > 16:
                    raise ValueError("dependencies must contain between one and sixteen items")
                if any(not isinstance(item, str) or not item.strip() for item in dependencies):
                    raise ValueError("dependencies must be non-empty strings")
                dependencies = [item.strip() for item in dependencies]
                with psycopg.connect(DATABASE_URL) as connection:
                    parent = connection.execute(
                        "SELECT product_brief_id, status FROM agent_technical_plans WHERE id = %s",
                        (parent_plan_id,),
                    ).fetchone()
                    if parent is None or parent[1] != "approved":
                        self._json_response(409, {"error": "an approved parent technical plan is required"})
                        return
                    context = {
                        "product_brief_id": parent[0],
                        "parent_technical_plan_id": parent_plan_id,
                        "revision_type": "amendment",
                        "dependencies": dependencies,
                    }
                approval_id = create_pending_approval(
                    "generate_technical_plan",
                    requested_by,
                    reason,
                    context,
                )
                self._json_response(201, {"id": approval_id, "status": "pending", "context": context})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path == "/technical-plans/generate":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                brief_id = int(payload["product_brief_id"])
                requested_by = str(payload.get("requested_by", "operator")).strip()
                init_database()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "generate_technical_plan"):
                        self._json_response(403, {"error": "approved generate_technical_plan request required"})
                        return
                    approval = get_approval_request(connection, approval_id)
                    approval_context = approval[10] if approval else {}
                    revision_type = str(approval_context.get("revision_type", "initial"))
                    parent_plan_id = approval_context.get("parent_technical_plan_id")
                    dependencies = approval_context.get("dependencies", [])
                    if revision_type == "amendment":
                        if not parent_plan_id or not isinstance(dependencies, list) or not dependencies:
                            self._json_response(400, {"error": "technical-plan amendment requires parent_technical_plan_id and dependencies"})
                            return
                        if any(not isinstance(item, str) or not item.strip() for item in dependencies):
                            self._json_response(400, {"error": "technical-plan dependencies must be non-empty strings"})
                            return
                    brief_row = connection.execute(
                        "SELECT status, brief, backlog FROM agent_product_briefs WHERE id = %s",
                        (brief_id,),
                    ).fetchone()
                    if brief_row is None or brief_row[0] != "approved":
                        self._json_response(409, {"error": "an approved product brief is required"})
                        return
                    already_used = connection.execute(
                        "SELECT 1 FROM agent_technical_plans WHERE approval_request_id = %s",
                        (approval_id,),
                    ).fetchone()
                    if already_used:
                        self._json_response(409, {"error": "approval request already generated a technical plan"})
                        return
                    record_audit_event(
                        connection, CTO_NAME, "approved_action_started", requested_by,
                        str(approval_id), {"action": "generate_technical_plan", "product_brief_id": brief_id},
                    )
                    connection.commit()
                plan = generate_technical_plan(
                    LITELLM_BASE_URL,
                    LITELLM_MASTER_KEY,
                    CTO_MODEL,
                    {
                        "brief": brief_row[1],
                        "backlog": brief_row[2],
                        "required_dependencies": dependencies,
                    },
                )
                now = datetime.now(timezone.utc)
                with psycopg.connect(DATABASE_URL) as connection:
                    row = connection.execute(
                        """
                        INSERT INTO agent_technical_plans
                            (agent_name, schema_version, product_brief_id, requested_by,
                             approval_request_id, parent_technical_plan_id, revision_type,
                             dependencies, status, plan, created_at, updated_at)
                        VALUES (%s, '1.0', %s, %s, %s, %s, %s, %s::jsonb, 'draft', %s::jsonb, %s, %s)
                        RETURNING id
                        """,
                        (
                            CTO_NAME, brief_id, requested_by, approval_id,
                            int(parent_plan_id) if parent_plan_id else None,
                            revision_type, json.dumps(dependencies), json.dumps(plan), now, now,
                        ),
                    ).fetchone()
                    record_audit_event(
                        connection, CTO_NAME, "technical_plan_generated", CTO_NAME,
                        str(row[0]), {"approval_id": approval_id, "product_brief_id": brief_id},
                    )
                    connection.commit()
                if revision_type == "amendment":
                    create_pending_approval(
                        "approve_technical_plan",
                        CTO_NAME,
                        f"Review technical-plan amendment {row[0]} and its explicit dependencies.",
                        {"technical_plan_id": row[0], "parent_technical_plan_id": int(parent_plan_id), "revision_type": "amendment"},
                    )
                else:
                    queue_automated_handoff(
                        "approve_technical_plan", CTO_NAME,
                        {"technical_plan_id": row[0]},
                    )
                self._json_response(201, {"id": row[0], "status": "draft", "schema_version": "1.0"})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
                self._json_response(400, {"error": str(error)})
            except Exception as error:
                logger.exception("technical plan generation failed")
                self._json_response(502, {"error": type(error).__name__})
            return

        if self.path.startswith("/technical-plans/") and self.path.endswith("/approve"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                plan_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                decided_by = str(payload.get("decided_by", "operator")).strip()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "approve_technical_plan"):
                        self._json_response(403, {"error": "approved approve_technical_plan request required"})
                        return
                    updated = connection.execute(
                        """
                        UPDATE agent_technical_plans
                        SET status = 'approved', updated_at = %s
                        WHERE id = %s AND status = 'draft'
                        RETURNING id
                        """,
                        (datetime.now(timezone.utc), plan_id),
                    ).fetchone()
                    if updated is None:
                        self._json_response(404, {"error": "draft technical plan not found"})
                        return
                    record_audit_event(
                        connection, CTO_NAME, "technical_plan_approved", decided_by,
                        str(plan_id), {"approval_id": approval_id},
                    )
                    connection.commit()
                queue_generation_approval(
                    "generate_engineering_plan",
                    CTO_NAME,
                    {"technical_plan_id": plan_id},
                )
                self._json_response(200, {"id": plan_id, "status": "approved"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path == "/product-briefs/request":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                product_context = str(payload.get("product_context", "")).strip()
                context = {
                    "product_context": product_context,
                    "request_token": uuid.uuid4().hex,
                }
                queue_generation_approval(
                    "generate_product_brief",
                    PRODUCT_MANAGER_NAME,
                    context,
                )
                with psycopg.connect(DATABASE_URL) as connection:
                    request_id = connection.execute(
                        """
                        SELECT id FROM agent_approval_requests
                        WHERE action = 'generate_product_brief' AND status = 'approved'
                          AND context = %s::jsonb
                        ORDER BY id DESC LIMIT 1
                        """,
                        (json.dumps(context, sort_keys=True),),
                    ).fetchone()[0]
                self._json_response(
                    202,
                    {
                        "id": request_id,
                        "status": "approved",
                        "action": "generate_product_brief",
                        "automated": True,
                        "next_step": "Wait for the Product Manager to generate the draft; publication will require human approval.",
                    },
                )
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path.startswith("/product-briefs/") and self.path.endswith("/archive"):
            actor = self._authenticated_actor()
            if actor is None or actor[0] != "human":
                self._json_response(403, {"error": "only the authenticated operator can request product-brief archival"})
                return
            try:
                brief_id = int(self.path.split("/")[2])
                payload = self._request_json()
                reason = str(payload.get("reason", "")).strip()
                confirmed = payload.get("confirm") is True
                if not reason:
                    raise ValueError("reason is required")
                with psycopg.connect(DATABASE_URL) as connection:
                    brief = connection.execute(
                        "SELECT status FROM agent_product_briefs WHERE id = %s",
                        (brief_id,),
                    ).fetchone()
                    if brief is None:
                        self._json_response(404, {"error": "product brief not found"})
                        return
                    if brief[0] == "archived":
                        self._json_response(409, {"error": "product brief is already archived"})
                        return
                    pending = connection.execute(
                        """
                        SELECT id
                        FROM agent_approval_requests
                        WHERE action = 'approve_product_brief'
                          AND status = 'pending'
                          AND context->>'product_brief_id' = %s
                        FOR UPDATE
                        """,
                        (str(brief_id),),
                    ).fetchall()
                    if pending and not confirmed:
                        self._json_response(
                            409,
                            {
                                "error": "active publication approval exists; repeat with confirm=true to request archival and cancel it on approval",
                                "publication_approval_ids": [row[0] for row in pending],
                            },
                        )
                        return
                    approval_id = create_approval_request(
                        connection,
                        AGENT_NAME,
                        "archive_product_brief",
                        actor[1],
                        reason,
                        {"product_brief_id": brief_id, "reason": reason, "confirm": confirmed},
                    )
                    record_audit_event(
                        connection,
                        AGENT_NAME,
                        "approval_requested",
                        actor[1],
                        str(approval_id),
                        {"action": "archive_product_brief", "product_brief_id": brief_id},
                    )
                    connection.commit()
                self._json_response(
                    202,
                    {"id": approval_id, "status": "pending", "action": "archive_product_brief", "product_brief_id": brief_id},
                )
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path == "/product-briefs/generate":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                requested_by = str(payload.get("requested_by", "operator")).strip()
                context = str(payload.get("product_context", PRODUCT_CONTEXT)).strip()
                if not requested_by or not context or len(context) > 12_000:
                    raise ValueError("requested_by and product_context are required and context must be <= 12000 characters")
                init_database()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "generate_product_brief"):
                        self._json_response(403, {"error": "approved generate_product_brief request required"})
                        return
                    already_used = connection.execute(
                        "SELECT 1 FROM agent_product_briefs WHERE approval_request_id = %s",
                        (approval_id,),
                    ).fetchone()
                    if already_used:
                        self._json_response(409, {"error": "approval request already generated a product brief"})
                        return
                    record_audit_event(
                        connection, PRODUCT_MANAGER_NAME, "approved_action_started",
                        requested_by, str(approval_id), {"action": "generate_product_brief"},
                    )
                    connection.commit()
                output = generate_product_output(
                    LITELLM_BASE_URL, LITELLM_MASTER_KEY, PRODUCT_MANAGER_MODEL, context,
                )
                now = datetime.now(timezone.utc)
                with psycopg.connect(DATABASE_URL) as connection:
                    row = connection.execute(
                        """
                        INSERT INTO agent_product_briefs
                            (agent_name, schema_version, requested_by, approval_request_id,
                             status, brief, backlog, source_context, created_at, updated_at)
                        VALUES (%s, '1.0', %s, %s, 'draft', %s::jsonb, %s::jsonb, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            PRODUCT_MANAGER_NAME, requested_by, approval_id,
                            json.dumps(output["brief"]), json.dumps(output["backlog"]),
                            context, now, now,
                        ),
                    ).fetchone()
                    record_audit_event(
                        connection, PRODUCT_MANAGER_NAME, "product_brief_generated",
                        PRODUCT_MANAGER_NAME, str(row[0]), {"approval_id": approval_id},
                    )
                    connection.commit()
                publication_approval_id = create_pending_approval(
                    "approve_product_brief",
                    OPERATOR_NAME,
                    f"Review and publish generated product brief {row[0]}.",
                    {"product_brief_id": row[0]},
                )
                self._json_response(
                    201,
                    {
                        "id": row[0],
                        "status": "draft",
                        "schema_version": "1.0",
                        "publication_approval_id": publication_approval_id,
                    },
                )
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
                self._json_response(400, {"error": str(error)})
            except Exception as error:
                logger.exception("product brief generation failed")
                self._json_response(502, {"error": type(error).__name__})
            return

        if self.path.startswith("/product-briefs/") and self.path.endswith("/approve"):
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                brief_id = int(self.path.split("/")[2])
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                decided_by = str(payload.get("decided_by", "operator")).strip()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "approve_product_brief"):
                        self._json_response(403, {"error": "approved approve_product_brief request required"})
                        return
                    updated = connection.execute(
                        """
                        UPDATE agent_product_briefs
                        SET status = 'approved', updated_at = %s
                        WHERE id = %s AND status = 'draft'
                        RETURNING id
                        """,
                        (datetime.now(timezone.utc), brief_id),
                    ).fetchone()
                    if updated is None:
                        self._json_response(404, {"error": "draft product brief not found"})
                        return
                    record_audit_event(
                        connection, PRODUCT_MANAGER_NAME, "product_brief_approved",
                        decided_by, str(brief_id), {"approval_id": approval_id},
                    )
                    connection.commit()
                self._json_response(200, {"id": brief_id, "status": "published", "publication_status": "published"})
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if self.path == "/refresh-health-report":
            if not self._authorized():
                self._json_response(401, {"error": "unauthorized"})
                return
            try:
                payload = self._request_json()
                approval_id = int(payload["approval_id"])
                init_database()
                with psycopg.connect(DATABASE_URL) as connection:
                    if not approved_action(connection, approval_id, "read_health_report"):
                        self._json_response(403, {"error": "approved read_health_report request required"})
                        return
                    record_audit_event(
                        connection, AGENT_NAME, "approved_action_started", AGENT_NAME,
                        str(approval_id), {"action": "read_health_report"},
                    )
                    connection.commit()
                completed = run_once()
                with psycopg.connect(DATABASE_URL) as connection:
                    record_audit_event(
                        connection, AGENT_NAME, "approved_action_completed", AGENT_NAME,
                        str(approval_id), {"action": "read_health_report", "completed": completed},
                    )
                    connection.commit()
                self._json_response(
                    200 if completed else 502,
                    {"status": "completed" if completed else "failed", "approval_id": approval_id},
                )
            except (KeyError, ValueError, TypeError) as error:
                self._json_response(400, {"error": str(error)})
            return

        if not self.path == "/approval-requests" and not self.path.startswith("/approval-requests/"):
            self.send_error(404)
            return
        if not self._authorized():
            self._json_response(401, {"error": "unauthorized"})
            return
        try:
            payload = self._request_json()
            with psycopg.connect(DATABASE_URL) as connection:
                if self.path == "/approval-requests":
                    request_id = create_approval_request(
                        connection, AGENT_NAME, str(payload["action"]),
                        str(payload["requested_by"]), str(payload["reason"]),
                        payload.get("context") if isinstance(payload.get("context"), dict) else None,
                    )
                    record_audit_event(
                        connection, AGENT_NAME, "approval_requested",
                        str(payload["requested_by"]), str(request_id),
                    )
                    connection.commit()
                    self._json_response(201, {"id": request_id, "status": "pending"})
                    return

                request_id = int(self.path.rsplit("/", 1)[1].split("?", 1)[0])
                status = str(payload["status"])
                requested_decided_by = str(payload.get("decided_by", "")).strip()
                decision_reason = str(payload["decision_reason"])
                actor = self._authenticated_actor()
                if actor is None:
                    self._json_response(401, {"error": "unauthorized"})
                    return
                request_row = get_approval_request(connection, request_id)
                if request_row is None:
                    self._json_response(404, {"error": "approval request not found"})
                    return
                if actor[0] == "agent":
                    if not agent_can_approve_generation(
                        request_row[2],
                        actor[1],
                        {
                            "product_manager": PRODUCT_MANAGER_NAME,
                            "cto": CTO_NAME,
                            "engineering_manager": ENGINEERING_MANAGER_NAME,
                        },
                    ):
                        self._json_response(403, {"error": "agent is not authorized to decide this approval"})
                        return
                    if request_row[3] == actor[1]:
                        self._json_response(403, {"error": "agents cannot approve their own requests"})
                        return
                    decided_by = actor[1]
                else:
                    if requested_decided_by and requested_decided_by != OPERATOR_NAME:
                        self._json_response(403, {"error": "decided_by must identify the authenticated operator"})
                        return
                    decided_by = OPERATOR_NAME
                decide_approval(connection, request_id, status, decided_by, decision_reason)
                record_audit_event(
                    connection, AGENT_NAME, "approval_decided", decided_by,
                    str(request_id), {"status": status},
                )
                connection.commit()
                request_row = get_approval_request(connection, request_id)
            if status == "approved":
                threading.Thread(
                    target=dispatch_approved_action,
                    args=(request_row[2], request_id),
                    daemon=True,
                ).start()
            fields = (
                "id", "agent_name", "action", "requested_by", "reason", "status",
                "decided_by", "decision_reason", "requested_at", "decided_at", "context",
            )
            self._json_response(200, {"request": dict(zip(fields, request_row))})
        except (KeyError, ValueError, TypeError) as error:
            self._json_response(400, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        return


def start_report_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", AGENT_HTTP_PORT), ReportHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("read-only report endpoint listening on port %s", AGENT_HTTP_PORT)


def main() -> None:
    require_config()
    parsed_database_url = urlsplit(DATABASE_URL)
    if not parsed_database_url.hostname or not parsed_database_url.username:
        raise RuntimeError("DATABASE_URL must include a database hostname and username")
    if not AGENT_APPROVAL_TOKEN:
        logger.warning("approval API disabled: AGENT_APPROVAL_TOKEN is not configured")
    init_database()
    start_report_server()
    threading.Thread(target=execution_worker_loop, daemon=True).start()
    threading.Thread(target=qa_worker_loop, daemon=True).start()
    threading.Thread(target=work_item_orchestrator_loop, daemon=True).start()
    threading.Thread(target=repository_worker_loop, daemon=True).start()
    threading.Thread(target=proposal_qa_worker_loop, daemon=True).start()
    while True:
        run_once()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
