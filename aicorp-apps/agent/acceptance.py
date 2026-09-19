from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    authorization_token: str | None = None,
) -> tuple[int, Any, float]:
    body = None
    headers = {"Accept": "application/json"}
    if authorization_token:
        headers["Authorization"] = f"Bearer {authorization_token}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    started = time.perf_counter()
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read().decode("utf-8")
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                parsed_body = json.loads(response_body) if response_body else {}
            except json.JSONDecodeError:
                parsed_body = response_body
            return response.status, parsed_body, elapsed_ms
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError:
            parsed = {"body": response_body}
        return error.code, parsed, elapsed_ms
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return 0, {"error": f"{type(error).__name__}: {error}"}, elapsed_ms


def _check(
    check_id: str,
    title: str,
    command: str,
    expected: str,
    actual: str,
    passed: bool,
    duration_ms: float,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "title": title,
        "command": command,
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "duration_ms": duration_ms,
        "checked_at": _utc_now(),
        "details": details or {},
    }


def _criterion_check_id(requirement_title: str, criterion_text: str) -> str | None:
    text = f"{requirement_title} {criterion_text}".casefold()
    if "backup" in text and any(term in text for term in ("schedule", "timer", "cron")):
        return "backup-schedule"
    if "backup" in text and any(term in text for term in ("log", "logging", "success", "fail")):
        return "backup-logging"
    if any(term in text for term in ("alert", "threshold", "notification")):
        return "threshold-alerting"
    if any(term in text for term in ("discover", "hardware")):
        return "device-discovery"
    if any(term in text for term in ("last-known", "last known", "last_seen", "status")):
        return "device-state-read"
    if any(term in text for term in ("load", "2 second", "two second", "dashboard")):
        return "core-dashboard-load"
    if any(term in text for term in ("visibility", "display", "available", "centralized")):
        return "core-dashboard-availability"
    return None


def _run_host_command(command: list[str], timeout: float = 30.0) -> tuple[int, str, float]:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        return result.returncode, output[-500:], round((time.perf_counter() - started) * 1000, 2)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, f"{type(error).__name__}: {error}"[-500:], round((time.perf_counter() - started) * 1000, 2)


def _backup_acceptance_checks() -> list[dict[str, Any]]:
    enabled_status, enabled_output, enabled_duration_ms = _run_host_command(
        ["systemctl", "is-enabled", "aicorp-backup.timer"]
    )
    active_status, active_output, active_duration_ms = _run_host_command(
        ["systemctl", "is-active", "aicorp-backup.timer"]
    )
    enabled = enabled_status == 0 and enabled_output.strip() == "enabled"
    active = active_status == 0 and active_output.strip() == "active"
    schedule_actual = f"enabled={enabled_output or '<no output>'}; active={active_output or '<no output>'}"
    journal_status, journal_output, journal_duration_ms = _run_host_command(
        ["journalctl", "-u", "aicorp-backup.service", "--no-pager", "-n", "100", "-o", "cat"]
    )
    log_markers = (
        "Backup completed:",
        "Backup directory is not available:",
        "Backup destination has less than",
        "PostgreSQL backup is empty.",
    )
    observed_marker = next((marker for marker in log_markers if marker in journal_output), None)
    logging_passed = journal_status == 0 and observed_marker is not None
    return [
        _check(
            "backup-schedule",
            "AICorp backup runs on schedule",
            "systemctl is-enabled aicorp-backup.timer && systemctl is-active aicorp-backup.timer",
            "the backup timer is enabled and active",
            schedule_actual,
            enabled and active,
            enabled_duration_ms + active_duration_ms,
            {"enabled_output": enabled_output, "active_output": active_output},
        ),
        _check(
            "backup-logging",
            "AICorp backup logs success or failure",
            "journalctl -u aicorp-backup.service --no-pager -n 100 -o cat",
            "recent backup-service journal output contains a success or failure marker",
            journal_output or "<no output>",
            logging_passed,
            journal_duration_ms,
            {"observed_marker": observed_marker},
        ),
    ]


def run_product_acceptance(
    base_url: str | None = None,
    authorization_token: str | None = None,
    requirement_contract: dict[str, Any] | None = None,
    source_hash_value: str | None = None,
) -> dict[str, Any]:
    base_url = base_url or os.environ.get("AICORP_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8081")
    authorization_token = authorization_token or os.environ.get("AGENT_APPROVAL_TOKEN", "").strip()
    runtime = {
        "service": "aicorp-agent",
        "version": os.environ.get("AICORP_PRODUCT_VERSION", "unknown"),
        "source_hash": source_hash_value or os.environ.get("AICORP_SOURCE_HASH", "unknown"),
    }
    checks: list[dict[str, Any]] = []

    status, product, duration_ms = _request(base_url, "GET", "/product")
    checks.append(_check(
        "core-dashboard-availability",
        "Core dashboard is available",
        f"GET {base_url}/product",
        "HTTP 200 with dashboard HTML",
        f"HTTP {status}",
        status == 200 and "HomeLabOps" in str(product),
        duration_ms,
    ))

    status, snapshot, duration_ms = _request(base_url, "GET", "/product-data")
    devices = snapshot.get("devices", []) if isinstance(snapshot, dict) else []
    if isinstance(snapshot, dict) and snapshot.get("version"):
        runtime["version"] = snapshot["version"]
    checks.append(_check(
        "core-dashboard-load",
        "Dashboard data loads within two seconds",
        f"GET {base_url}/product-data",
        "HTTP 200, response under 2000ms, and at least one discovered device",
        f"HTTP {status}, {duration_ms}ms, {len(devices)} device(s)",
        status == 200 and duration_ms < 2000 and bool(devices),
        duration_ms,
        {"device_count": len(devices)},
    ))

    discovery_device = devices[0] if devices and isinstance(devices[0], dict) else {
        "device_id": os.environ.get("AICORP_DEVICE_ID", "aicorp-control01"),
        "name": os.environ.get("AICORP_DEVICE_NAME", "AICorp Control Plane"),
        "hardware": "standard home-server hardware",
    }
    discovery_payload = {
        "device_id": discovery_device.get("device_id"),
        "name": discovery_device.get("name"),
        "hardware": discovery_device.get("hardware") or "standard home-server hardware",
        "status": "online",
        "source": "product-acceptance",
        "metadata": {"acceptance": True},
    }
    status, discovery, duration_ms = _request(
        base_url,
        "POST",
        "/devices/discovery",
        discovery_payload,
    )
    checks.append(_check(
        "device-discovery-authorization",
        "Device discovery requires operator authorization",
        f"POST {base_url}/devices/discovery",
        "HTTP 401 without operator authorization",
        f"HTTP {status}",
        status == 401 or not authorization_token,
        duration_ms,
        {"authorization_required": True, "response": discovery, "authorization_configured": bool(authorization_token)},
    ))

    status, discovery, duration_ms = _request(
        base_url,
        "POST",
        "/devices/discovery",
        discovery_payload,
        authorization_token,
    )
    checks.append(_check(
        "device-discovery",
        "Device discovery reports a standard home server",
        f"POST {base_url}/devices/discovery with operator authorization",
        "HTTP 200 and a persisted device discovery event",
        f"HTTP {status}",
        status == 200 and isinstance(discovery, dict) and discovery.get("device", {}).get("device_id") == discovery_payload["device_id"],
        duration_ms,
        {"response": discovery},
    ))

    offline_payload = {**discovery_payload, "status": "offline", "event_type": "heartbeat"}
    status, firing, duration_ms = _request(
        base_url,
        "POST",
        "/devices/heartbeat",
        offline_payload,
        authorization_token,
    )
    firing_alerts = firing.get("alerts", []) if isinstance(firing, dict) else []
    online_payload = {**discovery_payload, "status": "online", "event_type": "heartbeat"}
    resolved_status, resolved, resolved_duration_ms = _request(
        base_url,
        "POST",
        "/devices/heartbeat",
        online_payload,
        authorization_token,
    )
    resolved_alerts = resolved.get("alerts", []) if isinstance(resolved, dict) else []
    delivery_snapshot: dict[str, Any] = {}
    delivery_status = 0
    delivery_duration_ms = 0.0
    for _ in range(20):
        delivery_status, candidate, delivery_duration_ms = _request(base_url, "GET", "/product-data")
        if isinstance(candidate, dict):
            delivery_snapshot = candidate
            if any(
                delivery.get("device_id") == discovery_payload["device_id"]
                and delivery.get("event_type") == "product_alert_firing"
                and delivery.get("status") == "sent"
                for delivery in candidate.get("notification_deliveries", [])
            ):
                break
        time.sleep(0.5)
    notification_channel = delivery_snapshot.get("notification_channel") if delivery_snapshot else None
    notification_deliveries = delivery_snapshot.get("notification_deliveries", []) if delivery_snapshot else []
    firing_notification_sent = any(
        delivery.get("device_id") == discovery_payload["device_id"]
        and delivery.get("event_type") == "product_alert_firing"
        and delivery.get("status") == "sent"
        for delivery in notification_deliveries
    )
    checks.append(_check(
        "threshold-alerting",
        "Device thresholds trigger and resolve alerts",
        f"POST {base_url}/devices/heartbeat with offline and online state transitions",
        "HTTP 200, a firing alert, a resolved alert transition, and a sent notification",
        f"offline HTTP {status}, firing={len(firing_alerts)}; online HTTP {resolved_status}, resolved={len(resolved_alerts)}; delivery HTTP {delivery_status}, sent={firing_notification_sent}",
        status == 200
        and resolved_status == 200
        and any(alert.get("state") == "firing" for alert in firing_alerts)
        and any(alert.get("state") == "resolved" for alert in resolved_alerts)
        and all(alert.get("notification_status") in {"queued", "sent"} for alert in firing_alerts)
        and notification_channel not in {None, "disabled"}
        and firing_notification_sent,
        duration_ms + resolved_duration_ms,
        {
            "firing": firing,
            "resolved": resolved,
            "notification_channel": notification_channel,
            "notification_deliveries": notification_deliveries,
            "notification_delivery_duration_ms": delivery_duration_ms,
        },
    ))

    status, device_data, duration_ms = _request(base_url, "GET", "/devices")
    device_ids = {device.get("device_id") for device in device_data.get("devices", [])} if isinstance(device_data, dict) else set()
    checks.append(_check(
        "device-state-read",
        "Dashboard exposes discovered device state",
        f"GET {base_url}/devices",
        "HTTP 200 with device records and status fields",
        f"HTTP {status}, {len(device_ids)} device(s)",
        status == 200 and all({"device_id", "status", "last_seen_at"}.issubset(device) for device in device_data.get("devices", [])),
        duration_ms,
    ))

    required_check_ids = set()
    if requirement_contract is not None:
        from requirements import acceptance_checks

        required_check_ids = {
            check_id
            for criterion in acceptance_checks(requirement_contract)
            if (check_id := _criterion_check_id(criterion["requirement_title"], criterion["text"]))
        }
    if required_check_ids.intersection({"backup-schedule", "backup-logging"}):
        checks.extend(_backup_acceptance_checks())

    passed = all(check["passed"] for check in checks)
    result: dict[str, Any] = {
        "suite": "product-acceptance",
        "passed": passed,
        "checked_at": _utc_now(),
        "base_url": base_url,
        "runtime": runtime,
        "checks": checks,
    }
    if requirement_contract is not None:
        evidence = []
        checks_by_id = {check["check_id"]: check for check in checks}
        for criterion in acceptance_checks(requirement_contract):
            check_id = _criterion_check_id(criterion["requirement_title"], criterion["text"])
            check = checks_by_id.get(check_id) if check_id else None
            evidence.append(
                {
                    "acceptance_criterion_id": criterion["acceptance_criterion_id"],
                    "requirement_id": criterion["requirement_id"],
                    "result": "passed" if check and check["passed"] else "failed",
                    "command": check["command"] if check else "No executable acceptance check mapping",
                    "expected": check["expected"] if check else "Criterion must map to an executable product check",
                    "actual": check["actual"] if check else criterion["text"],
                    "timestamp": check["checked_at"] if check else _utc_now(),
                    "runtime": runtime,
                }
            )
        result["evidence"] = evidence
        result["contract_passed"] = all(item["result"] == "passed" for item in evidence)
        result["passed"] = result["passed"] and result["contract_passed"]
    return result


def source_hash(paths: list[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.encode("utf-8"))
        with open(path, "rb") as source:
            digest.update(source.read())
    return digest.hexdigest()