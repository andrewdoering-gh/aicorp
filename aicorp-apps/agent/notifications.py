"""Durable Teams notification delivery for workflow audit events."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

logger = logging.getLogger("aicorp-agent.notifications")
TEAMS_WEBHOOK_URL = os.environ.get("AICORP_TEAMS_WEBHOOK_URL", "").strip()
NOTIFICATION_POLL_SECONDS = max(1, int(os.environ.get("AICORP_NOTIFICATION_POLL_SECONDS", "5")))
NOTIFICATION_BATCH_SIZE = max(1, int(os.environ.get("AICORP_NOTIFICATION_BATCH_SIZE", "10")))
NOTIFICATION_MAX_ATTEMPTS = max(1, int(os.environ.get("AICORP_NOTIFICATION_MAX_ATTEMPTS", "10")))


def init_notification_tables(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_notification_outbox (
            id BIGSERIAL PRIMARY KEY,
            audit_event_id BIGINT NOT NULL REFERENCES agent_audit_events(id),
            payload JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL,
            last_error TEXT,
            sent_at TIMESTAMPTZ,
            CONSTRAINT agent_notification_status_check
                CHECK (status IN ('pending', 'sent', 'failed'))
        )
        """
    )
    connection.commit()


def enqueue_audit_notification(
    connection: psycopg.Connection,
    audit_event_id: int,
    payload: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO agent_notification_outbox (audit_event_id, payload, next_attempt_at)
        VALUES (%s, %s::jsonb, %s)
        """,
        (audit_event_id, json.dumps(payload, default=str), datetime.now(timezone.utc)),
    )


def _teams_payload(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") or {}
    detail_text = json.dumps(details, default=str, sort_keys=True)
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "Medium",
                            "weight": "Bolder",
                            "text": f"AICorp workflow: {event['event_type']}",
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Agent", "value": event["agent_name"]},
                                {"title": "Actor", "value": event["actor"]},
                                {
                                    "title": "Subject",
                                    "value": event.get("subject") or "-",
                                },
                                {"title": "Time", "value": event["occurred_at"]},
                            ],
                        },
                        {
                            "type": "TextBlock",
                            "text": f"Details: {detail_text}",
                            "wrap": True,
                        },
                    ],
                },
            }
        ],
    }


def _send(payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        TEAMS_WEBHOOK_URL,
        data=json.dumps(_teams_payload(payload)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def run_notification_worker(database_url: str) -> None:
    if not TEAMS_WEBHOOK_URL:
        logger.info("Teams notifications disabled: AICORP_TEAMS_WEBHOOK_URL is not configured")
        return
    while True:
        try:
            with psycopg.connect(database_url) as connection:
                init_notification_tables(connection)
            break
        except psycopg.Error:
            logger.exception("notification tables are not ready; retrying")
            time.sleep(NOTIFICATION_POLL_SECONDS)
    while True:
        sent_any = False
        try:
            with psycopg.connect(database_url) as connection:
                rows = connection.execute(
                    """
                    SELECT outbox.id, outbox.audit_event_id, outbox.payload, outbox.attempts
                    FROM agent_notification_outbox AS outbox
                    WHERE outbox.status = 'pending'
                      AND outbox.next_attempt_at <= %s
                    ORDER BY outbox.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (datetime.now(timezone.utc), NOTIFICATION_BATCH_SIZE),
                ).fetchall()
                for outbox_id, audit_event_id, event, attempts in rows:
                    try:
                        _send(event)
                        connection.execute(
                            """
                            UPDATE agent_notification_outbox
                            SET status = 'sent', sent_at = %s, last_error = NULL
                            WHERE id = %s
                            """,
                            (datetime.now(timezone.utc), outbox_id),
                        )
                        sent_any = True
                    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
                        next_attempts = attempts + 1
                        status = "failed" if next_attempts >= NOTIFICATION_MAX_ATTEMPTS else "pending"
                        delay = min(3600, 2 ** min(next_attempts, 10))
                        connection.execute(
                            """
                            UPDATE agent_notification_outbox
                            SET status = %s, attempts = %s, last_error = %s,
                                next_attempt_at = %s
                            WHERE id = %s
                            """,
                            (
                                status,
                                next_attempts,
                                f"{type(error).__name__}: {str(error)[:500]}",
                                datetime.now(timezone.utc) + timedelta(seconds=delay),
                                outbox_id,
                            ),
                        )
                        logger.warning(
                            "Teams notification failed: outbox_id=%s audit_event_id=%s attempt=%s error=%s",
                            outbox_id,
                            audit_event_id,
                            next_attempts,
                            type(error).__name__,
                        )
                connection.commit()
        except Exception as error:
            logger.exception("notification worker iteration failed: error=%s", type(error).__name__)
            time.sleep(NOTIFICATION_POLL_SECONDS)
            continue
        if not sent_any:
            time.sleep(NOTIFICATION_POLL_SECONDS)
