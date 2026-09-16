import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg

AUTHORIZED_TOOLS: frozenset[str] = frozenset()
APPROVAL_STATUSES = frozenset({"pending", "approved", "denied", "expired", "cancelled"})
DECISION_STATUSES = frozenset({"approved", "denied"})
APPROVAL_TTL_SECONDS = int(os.environ.get("AGENT_APPROVAL_TTL_SECONDS", "86400"))

GENERATION_APPROVERS = {
    "generate_technical_plan": "product_manager",
    "generate_engineering_plan": "cto",
    "generate_software_engineer_plan": "engineering_manager",
    "generate_qa_plan": "engineering_manager",
}
AUTO_DISPATCH_ACTIONS = frozenset(
    {
        "generate_product_brief",
        "generate_technical_plan",
        "generate_engineering_plan",
        "generate_software_engineer_plan",
        "generate_qa_plan",
    }
)
AUTOMATED_HANDOFF_ACTIONS = frozenset(
    {
        "approve_technical_plan",
        "approve_engineering_plan",
        "approve_software_engineer_plan",
        "approve_qa_plan",
        "start_software_engineer_execution",
        "submit_software_engineer_execution",
        "record_qa_validation",
        "submit_repository_change_proposal",
        "review_repository_change",
    }
)
HUMAN_GATE_ACTIONS = frozenset({"approve_product_brief", "archive_product_brief", "deploy"})


def init_governance_tables(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_audit_events (
            id BIGSERIAL PRIMARY KEY,
            agent_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            subject TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            occurred_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_approval_requests (
            id BIGSERIAL PRIMARY KEY,
            agent_name TEXT NOT NULL,
            action TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by TEXT,
            decision_reason TEXT,
            requested_at TIMESTAMPTZ NOT NULL,
            decided_at TIMESTAMPTZ,
            context JSONB NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT agent_approval_status_check
                CHECK (status IN ('pending', 'approved', 'denied', 'expired', 'cancelled'))
        )
        """
    )
    connection.execute(
        "ALTER TABLE agent_approval_requests ADD COLUMN IF NOT EXISTS context JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    connection.commit()


def record_audit_event(
    connection: psycopg.Connection,
    agent_name: str,
    event_type: str,
    actor: str,
    subject: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO agent_audit_events
            (agent_name, event_type, actor, subject, details, occurred_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            agent_name,
            event_type,
            actor,
            subject,
            json.dumps(details or {}),
            datetime.now(timezone.utc),
        ),
    )


def expire_pending_approvals(connection: psycopg.Connection, agent_name: str) -> int:
    rows = connection.execute(
        """
        UPDATE agent_approval_requests
        SET status = 'expired', decided_at = %s,
            decision_reason = 'Approval request expired.'
        WHERE agent_name = %s
          AND status = 'pending'
          AND requested_at < %s - (%s * INTERVAL '1 second')
        RETURNING id
        """,
        (datetime.now(timezone.utc), agent_name, datetime.now(timezone.utc), APPROVAL_TTL_SECONDS),
    ).fetchall()
    for row in rows:
        record_audit_event(
            connection,
            agent_name,
            "approval_expired",
            agent_name,
            subject=str(row[0]),
            details={"ttl_seconds": APPROVAL_TTL_SECONDS},
        )
    connection.commit()
    return len(rows)


def tool_is_authorized(tool_name: str) -> bool:
    return tool_name in AUTHORIZED_TOOLS


def validate_approval_decision(status: str) -> None:
    if status not in DECISION_STATUSES:
        raise ValueError(f"invalid approval decision: {status}")


def agent_can_approve_generation(action: str, agent_name: str, agent_names: dict[str, str]) -> bool:
    role = GENERATION_APPROVERS.get(action)
    return role is not None and agent_names.get(role) == agent_name


def create_approval_request(
    connection: psycopg.Connection,
    agent_name: str,
    action: str,
    requested_by: str,
    reason: str,
    context: dict[str, Any] | None = None,
) -> int:
    if not action or not requested_by or not reason:
        raise ValueError("approval action, requester, and reason are required")
    row = connection.execute(
        """
        INSERT INTO agent_approval_requests
            (agent_name, action, requested_by, reason, context, requested_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        RETURNING id
        """,
        (agent_name, action, requested_by, reason, json.dumps(context or {}), datetime.now(timezone.utc)),
    ).fetchone()
    connection.commit()
    return row[0]


def decide_approval(
    connection: psycopg.Connection,
    request_id: int,
    status: str,
    decided_by: str,
    decision_reason: str,
) -> None:
    validate_approval_decision(status)
    if not decided_by or not decision_reason:
        raise ValueError("decision maker and reason are required")
    updated = connection.execute(
        """
        UPDATE agent_approval_requests
        SET status = %s, decided_by = %s, decision_reason = %s, decided_at = %s
        WHERE id = %s AND status = 'pending'
        """,
        (status, decided_by, decision_reason, datetime.now(timezone.utc), request_id),
    ).rowcount
    if updated != 1:
        raise ValueError("approval request is missing or no longer pending")
    connection.commit()


def get_approval_request(connection: psycopg.Connection, request_id: int):
    return connection.execute(
        """
        SELECT id, agent_name, action, requested_by, reason, status,
             decided_by, decision_reason, requested_at, decided_at, context
        FROM agent_approval_requests
        WHERE id = %s
        """,
        (request_id,),
    ).fetchone()


def approved_action(connection: psycopg.Connection, request_id: int, action: str) -> bool:
    expire_pending_approvals(connection, os.environ.get("AGENT_NAME", "aicorp-health-summary"))
    row = connection.execute(
        """
        SELECT 1
        FROM agent_approval_requests
        WHERE id = %s AND action = %s AND status = 'approved'
        """,
        (request_id, action),
    ).fetchone()
    return row is not None
