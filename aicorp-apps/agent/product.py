from __future__ import annotations

import os
import platform
import socket
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


DEVICE_STATUSES = {"online", "offline", "unknown"}
ALERT_STATES = {"firing", "resolved"}
NOTIFICATION_STATES = {"queued", "sent", "failed", "disabled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("observed_at must be an ISO timestamp")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _host_value(path: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    return " ".join(value.split())[:160]


def _host_memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return None


def init_product_tables(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS homelab_devices (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            hardware TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown',
            last_seen_at TIMESTAMPTZ,
            discovered_at TIMESTAMPTZ NOT NULL,
            source TEXT NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT homelab_device_status_check
                CHECK (status IN ('online', 'offline', 'unknown'))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS homelab_discovery_events (
            id BIGSERIAL PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES homelab_devices(device_id),
            event_type TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS homelab_alerts (
            alert_key TEXT PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES homelab_devices(device_id),
            severity TEXT NOT NULL,
            state TEXT NOT NULL,
            threshold_seconds INTEGER NOT NULL,
            triggered_at TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            notification_status TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT homelab_alert_state_check
                CHECK (state IN ('firing', 'resolved')),
            CONSTRAINT homelab_alert_notification_check
                CHECK (notification_status IN ('queued', 'sent', 'failed', 'disabled'))
        )
        """
    )
    connection.commit()


def local_device_event() -> dict[str, Any]:
    device_id = os.environ.get("AICORP_DEVICE_ID", "aicorp-control01").strip() or "aicorp-control01"
    name = os.environ.get("AICORP_DEVICE_NAME", device_id).strip() or device_id
    system = platform.uname()
    vendor = _host_value("/sys/class/dmi/id/sys_vendor")
    product_name = _host_value("/sys/class/dmi/id/product_name")
    model = " ".join(value for value in (vendor, product_name) if value) or system.machine
    cpu_count = os.cpu_count() or 1
    return {
        "device_id": device_id,
        "name": name,
        "hardware": f"{model} ({system.machine})".strip(),
        "status": "online",
        "source": "aicorp-device-discovery",
        "event_type": "device_discovered",
        "observed_at": _now(),
        "metadata": {
            "hostname": socket.gethostname(),
            "processor": system.processor or "unknown",
            "hardware_class": "standard-home-server",
            "cpu_count": cpu_count,
            "memory_bytes": _host_memory_bytes(),
            "python": platform.python_version(),
        },
    }


def record_discovery_event(connection: psycopg.Connection, event: dict[str, Any]) -> dict[str, Any]:
    device_id = str(event.get("device_id", "")).strip()
    name = str(event.get("name", device_id)).strip() or device_id
    hardware = str(event.get("hardware", "unknown")).strip() or "unknown"
    status = str(event.get("status", "unknown")).strip().lower()
    source = str(event.get("source", "unknown")).strip() or "unknown"
    event_type = str(event.get("event_type", "device_discovered")).strip() or "device_discovered"
    if not device_id or len(device_id) > 160:
        raise ValueError("device_id must be a non-empty value of at most 160 characters")
    if status not in DEVICE_STATUSES:
        raise ValueError("status must be online, offline, or unknown")
    observed_at = _parse_timestamp(event.get("observed_at"))
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    current = _now()
    connection.execute(
        """
        INSERT INTO homelab_devices
            (device_id, name, hardware, status, last_seen_at, discovered_at, source, metadata, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (device_id) DO UPDATE SET
            name = EXCLUDED.name,
            hardware = EXCLUDED.hardware,
            status = EXCLUDED.status,
            last_seen_at = EXCLUDED.last_seen_at,
            source = EXCLUDED.source,
            metadata = EXCLUDED.metadata,
            updated_at = EXCLUDED.updated_at
        """,
        (
            device_id,
            name,
            hardware,
            status,
            observed_at,
            observed_at,
            source,
            Jsonb(metadata),
            current,
        ),
    )
    connection.execute(
        """
        INSERT INTO homelab_discovery_events
            (device_id, event_type, observed_at, payload, created_at)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        """,
        (
            device_id,
            event_type,
            observed_at,
            Jsonb({**event, "observed_at": observed_at.isoformat()}),
            current,
        ),
    )
    return {
        "device_id": device_id,
        "name": name,
        "hardware": hardware,
        "status": status,
        "last_seen_at": observed_at.isoformat(),
        "source": source,
        "event_type": event_type,
    }


def ensure_local_device(connection: psycopg.Connection) -> dict[str, Any]:
    return record_discovery_event(connection, local_device_event())


def evaluate_alerts(connection: psycopg.Connection) -> list[dict[str, Any]]:
    threshold_seconds = max(1, int(os.environ.get("AICORP_DEVICE_STALE_SECONDS", "300")))
    notification_enabled = bool(os.environ.get("AICORP_TEAMS_WEBHOOK_URL", "").strip())
    current = _now()
    rows = connection.execute(
        "SELECT device_id, status, last_seen_at FROM homelab_devices"
    ).fetchall()
    transitions: list[dict[str, Any]] = []
    for device_id, status, last_seen_at in rows:
        stale = (
            status != "online"
            or last_seen_at is None
            or last_seen_at < current - timedelta(seconds=threshold_seconds)
        )
        desired_state = "firing" if stale else "resolved"
        alert_key = f"device-stale:{device_id}"
        existing = connection.execute(
            "SELECT state FROM homelab_alerts WHERE alert_key = %s",
            (alert_key,),
        ).fetchone()
        if existing is None and desired_state == "resolved":
            continue
        if existing is not None and existing[0] == desired_state:
            continue
        notification_status = "queued" if notification_enabled else "disabled"
        connection.execute(
            """
            INSERT INTO homelab_alerts
                (alert_key, device_id, severity, state, threshold_seconds,
                 triggered_at, resolved_at, notification_status, updated_at)
            VALUES (%s, %s, 'warning', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (alert_key) DO UPDATE SET
                state = EXCLUDED.state,
                threshold_seconds = EXCLUDED.threshold_seconds,
                triggered_at = EXCLUDED.triggered_at,
                resolved_at = EXCLUDED.resolved_at,
                notification_status = EXCLUDED.notification_status,
                updated_at = EXCLUDED.updated_at
            """,
            (
                alert_key,
                device_id,
                desired_state,
                threshold_seconds,
                current if desired_state == "firing" else None,
                current if desired_state == "resolved" else None,
                notification_status,
                current,
            ),
        )
        transitions.append(
            {
                "alert_key": alert_key,
                "device_id": device_id,
                "state": desired_state,
                "severity": "warning",
                "threshold_seconds": threshold_seconds,
                "notification_status": notification_status,
            }
        )
    return transitions


def device_snapshot(connection: psycopg.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT device_id, name, hardware, status, last_seen_at,
               discovered_at, source, metadata
        FROM homelab_devices
        ORDER BY name, device_id
        """
    ).fetchall()
    fields = (
        "device_id", "name", "hardware", "status", "last_seen_at",
        "discovered_at", "source", "metadata",
    )
    return [
        {
            **dict(zip(fields, row)),
            "last_seen_at": row[4].isoformat() if row[4] else None,
            "discovered_at": row[5].isoformat() if row[5] else None,
        }
        for row in rows
    ]


def discovery_event_count(connection: psycopg.Connection) -> int:
    return int(connection.execute("SELECT count(*) FROM homelab_discovery_events").fetchone()[0])


def alert_snapshot(connection: psycopg.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT alert_key, device_id, severity, state, threshold_seconds,
               triggered_at, resolved_at, notification_status, updated_at
        FROM homelab_alerts
        WHERE state = 'firing'
        ORDER BY triggered_at DESC NULLS LAST, alert_key
        """
    ).fetchall()
    fields = (
        "alert_key", "device_id", "severity", "state", "threshold_seconds",
        "triggered_at", "resolved_at", "notification_status", "updated_at",
    )
    return [
        {
            **dict(zip(fields, row)),
            "triggered_at": row[5].isoformat() if row[5] else None,
            "resolved_at": row[6].isoformat() if row[6] else None,
            "updated_at": row[8].isoformat() if row[8] else None,
        }
        for row in rows
    ]


def notification_snapshot(connection: psycopg.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT audit.subject, audit.event_type, outbox.status, outbox.attempts,
               outbox.last_error, outbox.sent_at
        FROM agent_notification_outbox AS outbox
        JOIN agent_audit_events AS audit ON audit.id = outbox.audit_event_id
        WHERE audit.event_type IN ('product_alert_firing', 'product_alert_resolved')
        ORDER BY audit.occurred_at DESC, outbox.id DESC
        LIMIT 20
        """
    ).fetchall()
    fields = ("device_id", "event_type", "status", "attempts", "last_error", "sent_at")
    return [
        {
            **dict(zip(fields, row)),
            "sent_at": row[5].isoformat() if row[5] else None,
        }
        for row in rows
    ]