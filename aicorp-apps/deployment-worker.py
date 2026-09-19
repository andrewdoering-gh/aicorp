#!/usr/bin/env python3
"""Execute one approved, bounded AICorp deployment request at a time."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import urllib.error
import urllib.request

import psycopg

from agent.workstreams import WORKSTREAM_PREDECESSORS

_database_url = urlsplit(os.environ["DATABASE_URL"])
if _database_url.hostname == "postgres":
    userinfo, hostport = _database_url.netloc.rsplit("@", 1)
    hostport = "127.0.0.1" + (hostport[len("postgres"):] if hostport.startswith("postgres") else "")
    _database_url = _database_url._replace(netloc=f"{userinfo}@{hostport}")
DATABASE_URL = urlunsplit(_database_url)
POLL_SECONDS = int(os.environ.get("DEPLOYMENT_WORKER_INTERVAL_SECONDS", "30"))
STALE_RUN_SECONDS = int(os.environ.get("DEPLOYMENT_WORKER_STALE_RUN_SECONDS", "900"))
REPOSITORY_ROOT = Path(os.environ.get("AICORP_DEPLOYMENT_ROOT", "/opt/aicorp"))
COMPOSE_FILE = Path(os.environ.get("AICORP_COMPOSE_FILE", "/opt/aicorp/compose.yaml"))
REPOSITORY_PREFIX = "aicorp/"
AGENT_ROOT = REPOSITORY_ROOT / "agent"
ACCEPTANCE_BASE_URL = os.environ.get("AICORP_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8081")
AGENT_APPROVAL_TOKEN = os.environ.get("AGENT_APPROVAL_TOKEN", "").strip()
PRODUCT_SERVICE = os.environ.get("AICORP_PRODUCT_SERVICE", "aicorp-agent")
logger = logging.getLogger("aicorp-deployment-worker")

if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from acceptance import run_product_acceptance
from requirements import (
    acceptance_evidence_passed,
    apply_acceptance_evidence,
    validate_requirement_contract,
)


class DeploymentAcceptanceError(RuntimeError):
    def __init__(self, message: str, evidence: dict[str, object]):
        super().__init__(message)
        self.evidence = evidence


def now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_tables(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_deployment_runs (
            approval_id BIGINT PRIMARY KEY REFERENCES agent_approval_requests(id),
            proposal_id BIGINT NOT NULL REFERENCES agent_change_proposals(id),
            status TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            CONSTRAINT agent_deployment_run_status_check
                CHECK (status IN ('running', 'completed', 'failed'))
        )
        """
    )
    connection.commit()


def predecessor_workstreams_deployed(connection: psycopg.Connection, proposal_id: int) -> bool:
        row = connection.execute(
                """
                SELECT task.worker_plan_id, task.work_item_id
                FROM agent_change_proposals AS proposal
                JOIN agent_execution_tasks AS task ON task.id = proposal.execution_task_id
                WHERE proposal.id = %s
                """,
                (proposal_id,),
        ).fetchone()
        if row is None:
                raise RuntimeError(f"deployment proposal {proposal_id} has no execution task")
        worker_plan_id, work_item_id = row
        predecessors = WORKSTREAM_PREDECESSORS.get(str(work_item_id))
        if predecessors is None:
                raise RuntimeError(f"unknown deployment workstream: {work_item_id}")
        for predecessor in predecessors:
                completed = connection.execute(
                        """
                        SELECT 1
                        FROM agent_execution_tasks AS predecessor_task
                        JOIN agent_change_proposals AS predecessor_proposal
                            ON predecessor_proposal.execution_task_id = predecessor_task.id
                        JOIN agent_approval_requests AS predecessor_approval
                            ON predecessor_approval.action IN ('deploy', 'retry_deployment')
                         AND (predecessor_approval.context->>'proposal_id')::bigint = predecessor_proposal.id
                        JOIN agent_deployment_runs AS predecessor_deployment
                            ON predecessor_deployment.approval_id = predecessor_approval.id
                        WHERE predecessor_task.worker_plan_id = %s
                            AND predecessor_task.work_item_id = %s
                            AND predecessor_deployment.status = 'completed'
                        LIMIT 1
                        """,
                        (worker_plan_id, predecessor),
                ).fetchone()
                if completed is None:
                        return False
        return True


def claim_request() -> tuple[int, int] | None:
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            """
                        SELECT approval.id, proposal.id
            FROM agent_approval_requests AS approval
            JOIN agent_change_proposals AS proposal
                            ON proposal.id::text = approval.context->>'proposal_id'
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
                                    JOIN agent_planning_state AS planning_state
                                        ON planning_state.id = 1
                        WHERE approval.action IN ('deploy', 'retry_deployment')
              AND approval.status = 'approved'
              AND proposal.status = 'approved'
                            AND brief.status <> 'archived'
                          AND (
                              approval.context->>'planning_generation' = planning_state.generation::text
                              OR (
                                  planning_state.generation = 1
                                  AND NOT (approval.context ? 'planning_generation')
                              )
                          )
                            AND (
                                    approval.action = 'deploy'
                                    OR (
                                            approval.action = 'retry_deployment'
                                            AND EXISTS (
                                                    SELECT 1
                                                    FROM agent_deployment_runs AS source_run
                                                    JOIN agent_approval_requests AS source_approval
                                                        ON source_approval.id = source_run.approval_id
                                                    WHERE source_run.approval_id::text = approval.context->>'source_deployment_approval_id'
                                                        AND source_run.status = 'failed'
                                                        AND source_approval.status = 'approved'
                                                        AND (
                                                            source_run.proposal_id::text = approval.context->>'source_proposal_id'
                                                            OR (
                                                                NOT (approval.context ? 'source_proposal_id')
                                                                AND source_run.proposal_id = proposal.id
                                                            )
                                                        )
                                                        AND (
                                                            NOT (approval.context ? 'source_proposal_id')
                                                            OR EXISTS (
                                                                SELECT 1
                                                                FROM agent_approval_requests AS remediation_start
                                                                JOIN agent_execution_tasks AS remediation_task
                                                                    ON remediation_task.start_approval_id = remediation_start.id
                                                                WHERE remediation_start.action = 'start_software_engineer_execution'
                                                                    AND remediation_start.status = 'approved'
                                                                    AND remediation_task.id = task.id
                                                                    AND remediation_start.context->>'deployment_remediation_for_approval_id'
                                                                        = approval.context->>'source_deployment_approval_id'
                                                                    AND remediation_start.context->>'deployment_remediation_for_proposal_id'
                                                                        = approval.context->>'source_proposal_id'
                                                            )
                                                        )
                                            )
                                    )
                            )
                            AND NOT EXISTS (
                                    SELECT 1 FROM agent_deployment_runs AS run
                                    WHERE run.approval_id = approval.id
                            )
            ORDER BY CASE task.work_item_id
                WHEN 'WS-01' THEN 1
                WHEN 'WS-02' THEN 2
                WHEN 'WS-03' THEN 3
                ELSE 99
            END, approval.id
            FOR UPDATE OF planning_state, approval, proposal SKIP LOCKED
            LIMIT 1
            """,
        ).fetchone()
        if row is None:
            return None
        approval_id, proposal_id = row
        if not predecessor_workstreams_deployed(connection, int(proposal_id)):
            connection.rollback()
            return None
        inserted = connection.execute(
            """
            INSERT INTO agent_deployment_runs
                (approval_id, proposal_id, status, started_at)
            VALUES (%s, %s, 'running', %s)
            ON CONFLICT (approval_id) DO NOTHING
            RETURNING approval_id
            """,
            (approval_id, proposal_id, now()),
        ).fetchone()
        connection.commit()
        return (approval_id, proposal_id) if inserted else None


def proposal_patch(proposal_id: int) -> tuple[str, list[str]]:
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            "SELECT proposal FROM agent_change_proposals WHERE id = %s AND status = 'approved'",
            (proposal_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("deployment proposal is not QA-passed")
    proposal = row[0]
    files = proposal.get("files")
    if not isinstance(files, list) or not files or any(
        not isinstance(path, str)
        or not path.startswith(REPOSITORY_PREFIX)
        or ".." in path.split("/")
        or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", path)
        for path in files
    ):
        raise RuntimeError("deployment proposal contains an unsafe repository path")
    patch = str(proposal.get("patch", ""))
    if not patch.startswith("diff --git "):
        raise RuntimeError("deployment proposal is not a unified diff")
    patch_paths = [source for source, target in re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$", patch, re.MULTILINE)]
    patch_targets = [target for source, target in re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$", patch, re.MULTILINE)]
    if not patch_paths or patch_paths != patch_targets or set(patch_paths) != set(files):
        raise RuntimeError("deployment patch paths do not match approved repository files")
    return patch, files


def run_checks(surfaces: list[str]) -> list[str]:
    target_files = [REPOSITORY_ROOT / path.removeprefix("aicorp/") for path in surfaces]
    missing = [str(path) for path in target_files if not path.is_file()]
    if missing:
        raise RuntimeError("deployment checks failed; missing approved files: " + ", ".join(missing))
    for path in target_files:
        path.read_text(encoding="utf-8")
    return ["all approved target files exist", "all approved target files are valid UTF-8"]


def product_contract(proposal_id: int) -> tuple[int, str, dict[str, object]]:
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            """
            SELECT brief.id, task.work_item_id, brief.requirements
            FROM agent_change_proposals AS proposal
            JOIN agent_execution_tasks AS task ON task.id = proposal.execution_task_id
            JOIN agent_worker_plans AS worker ON worker.id = task.worker_plan_id
            JOIN agent_engineering_plans AS engineering ON engineering.id = worker.engineering_plan_id
            JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
            JOIN agent_product_briefs AS brief ON brief.id = technical.product_brief_id
            WHERE proposal.id = %s
            """,
            (proposal_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"proposal {proposal_id} has no Product Brief lineage")
    brief_id, work_item_id, stored_contract = row
    return int(brief_id), str(work_item_id), validate_requirement_contract(stored_contract, int(brief_id))


def deployed_source_hash(surfaces: list[str]) -> str:
    paths = {
        REPOSITORY_ROOT / surface.removeprefix(REPOSITORY_PREFIX)
        for surface in surfaces
    }
    paths.update(
        {
            REPOSITORY_ROOT / "agent" / "agent.py",
            REPOSITORY_ROOT / "agent" / "product.py",
            REPOSITORY_ROOT / "agent" / "acceptance.py",
            REPOSITORY_ROOT / "agent" / "homelabops.html",
            REPOSITORY_ROOT / "agent" / "Dockerfile",
            REPOSITORY_ROOT / "compose.yaml",
        }
    )
    digest = hashlib.sha256()
    for path in sorted(paths, key=str):
        if path.is_file():
            digest.update(str(path.relative_to(REPOSITORY_ROOT)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def wait_for_agent() -> float:
    started = time.monotonic()
    last_error = "agent did not become healthy"
    for _ in range(30):
        try:
            request = urllib.request.Request(f"{ACCEPTANCE_BASE_URL.rstrip('/')}/health", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return round((time.monotonic() - started) * 1000, 2)
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(2)
    raise RuntimeError(f"agent did not become healthy after restart: {last_error}")


def request_deployment_remediation(approval_id: int, proposal_id: int) -> dict[str, object]:
    if not AGENT_APPROVAL_TOKEN:
        raise RuntimeError("AGENT_APPROVAL_TOKEN is required for deployment remediation handoff")
    request_body = json.dumps(
        {
            "source_deployment_approval_id": approval_id,
            "source_proposal_id": proposal_id,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{ACCEPTANCE_BASE_URL.rstrip('/')}/deployment-failures/remediate",
        data=request_body,
        headers={
            "Authorization": f"Bearer {AGENT_APPROVAL_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def handoff_failed_deployment(approval_id: int, proposal_id: int) -> None:
    try:
        logger.info(
            "queuing responsible-agent remediation: approval_id=%s proposal_id=%s result=%s",
            approval_id,
            proposal_id,
            request_deployment_remediation(approval_id, proposal_id),
        )
    except Exception as remediation_error:
        logger.exception(
            "responsible-agent remediation handoff failed: approval_id=%s proposal_id=%s error=%s",
            approval_id,
            proposal_id,
            type(remediation_error).__name__,
        )


def deploy(approval_id: int, proposal_id: int) -> dict[str, object]:
    brief_id, work_item_id, requirement_contract = product_contract(proposal_id)
    patch, surfaces = proposal_patch(proposal_id)
    backups: list[tuple[Path, Path]] = []
    for surface in surfaces:
        target = REPOSITORY_ROOT / surface.removeprefix("aicorp/")
        backup = Path(tempfile.mkstemp(prefix="aicorp-deploy-", suffix=target.suffix)[1])
        shutil.copy2(target, backup)
        backups.append((target, backup))
    try:
        for target, _ in backups:
            target.write_bytes(target.read_bytes().replace(b"\r", b""))
        patch = patch.replace("\r", "").rstrip("\n") + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as patch_file:
            patch_file.write(patch)
            patch_path = Path(patch_file.name)
        result = subprocess.run(
            ["patch", "--batch", "--forward", "--ignore-whitespace", "-p2", "-i", str(patch_path)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        patch_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError("bounded patch failed: " + result.stderr.strip())
        checks = run_checks(surfaces)
        subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "build", "agent"], check=True, timeout=300)
        subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "agent"], check=True, timeout=180)
        health_duration_ms = wait_for_agent()
        source_hash = deployed_source_hash(surfaces)
        acceptance = run_product_acceptance(
            ACCEPTANCE_BASE_URL,
            AGENT_APPROVAL_TOKEN,
            requirement_contract,
            source_hash,
        )
        evidence = {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "product_brief_id": brief_id,
            "work_item_id": work_item_id,
            "checks": checks,
            "targets": surfaces,
            "health_check": {
                "command": f"GET {ACCEPTANCE_BASE_URL.rstrip('/')}/health",
                "expected": "HTTP 200 after the agent restart",
                "actual": f"HTTP 200 in {health_duration_ms}ms",
                "timestamp": now().isoformat(),
            },
            "runtime": {
                "service": PRODUCT_SERVICE,
                "version": os.environ.get("AICORP_PRODUCT_VERSION", "unknown"),
                "source_hash": source_hash,
            },
            "acceptance": acceptance,
            "result": "deployed",
        }
        if not acceptance.get("passed"):
            raise DeploymentAcceptanceError(
                "deployed runtime failed Product Brief acceptance checks",
                evidence,
            )
        return evidence
    except Exception:
        for target, backup in backups:
            shutil.copy2(backup, target)
        try:
            subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "build", "agent"], check=True, timeout=300)
            subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "agent"], check=True, timeout=180)
        except Exception as rollback_error:
            raise RuntimeError(
                f"deployment failed and rollback restart failed: {type(rollback_error).__name__}: {rollback_error}"
            ) from rollback_error
        raise
    finally:
        for _, backup in backups:
            backup.unlink(missing_ok=True)


def reconcile_workflow_completion(connection: psycopg.Connection, proposal_id: int) -> None:
    """Mark completed workflow parents after every child task is deployed."""
    lineage = connection.execute(
        """
        SELECT task.id, worker.engineering_plan_id, engineering.technical_plan_id,
               technical.product_brief_id
        FROM agent_change_proposals AS proposal
        JOIN agent_execution_tasks AS task ON task.id = proposal.execution_task_id
        JOIN agent_worker_plans AS worker ON worker.id = task.worker_plan_id
        JOIN agent_engineering_plans AS engineering ON engineering.id = worker.engineering_plan_id
        JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
        WHERE proposal.id = %s
        """,
        (proposal_id,),
    ).fetchone()
    if lineage is None:
        raise RuntimeError(f"proposal {proposal_id} has no workflow lineage")
    task_id, engineering_plan_id, technical_plan_id, product_brief_id = lineage
    requirement_row = connection.execute(
        "SELECT requirements FROM agent_product_briefs WHERE id = %s",
        (product_brief_id,),
    ).fetchone()
    if requirement_row is None:
        raise RuntimeError(f"product brief {product_brief_id} has no requirement contract")
    try:
        requirement_contract = validate_requirement_contract(requirement_row[0], int(product_brief_id))
    except ValueError:
        return
    if not all(
        item["status"] == "done"
        for item in requirement_contract["items"]
    ) or not all(
        goal["status"] == "achieved"
        for goal in requirement_contract["goals"]
    ):
        return
    completed_at = now()
    connection.execute(
        """
        UPDATE agent_execution_tasks
        SET status = 'completed', updated_at = %s
        WHERE id = %s AND status = 'qa_passed'
        """,
        (completed_at, task_id),
    )
    connection.execute(
        """
        UPDATE agent_worker_plans AS worker
        SET status = 'completed', updated_at = %s
        WHERE worker.engineering_plan_id = %s AND worker.status = 'approved'
          AND EXISTS (
              SELECT 1 FROM agent_execution_tasks AS task
              WHERE task.worker_plan_id = worker.id OR task.qa_plan_id = worker.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM agent_execution_tasks AS task
              WHERE (task.worker_plan_id = worker.id OR task.qa_plan_id = worker.id)
                AND task.status <> 'completed'
          )
        RETURNING worker.id
        """,
        (completed_at, engineering_plan_id),
    ).fetchall()
    connection.execute(
        """
        UPDATE agent_engineering_plans AS engineering
        SET status = 'completed', updated_at = %s
        WHERE engineering.id = %s AND engineering.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_worker_plans AS worker
              WHERE worker.engineering_plan_id = engineering.id AND worker.status <> 'completed'
          )
        """,
        (completed_at, engineering_plan_id),
    )
    connection.execute(
        """
        UPDATE agent_technical_plans AS technical
        SET status = 'completed', updated_at = %s
        WHERE technical.id = %s AND technical.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_engineering_plans AS engineering
              WHERE engineering.technical_plan_id = technical.id AND engineering.status <> 'completed'
          )
        """,
        (completed_at, technical_plan_id),
    )
    connection.execute(
        """
        UPDATE agent_product_briefs AS brief
        SET status = 'completed', updated_at = %s
        WHERE brief.id = %s AND brief.status = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM agent_technical_plans AS technical
              WHERE technical.product_brief_id = brief.id AND technical.status <> 'completed'
          )
        """,
        (completed_at, product_brief_id),
    )


def record_result(approval_id: int, proposal_id: int, status: str, evidence: dict[str, object]) -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        lineage = connection.execute(
            """
            SELECT technical.product_brief_id, brief.requirements, brief.backlog
            FROM agent_change_proposals AS proposal
            JOIN agent_execution_tasks AS task ON task.id = proposal.execution_task_id
            JOIN agent_worker_plans AS worker ON worker.id = task.worker_plan_id
            JOIN agent_engineering_plans AS engineering ON engineering.id = worker.engineering_plan_id
            JOIN agent_technical_plans AS technical ON technical.id = engineering.technical_plan_id
            JOIN agent_product_briefs AS brief ON brief.id = technical.product_brief_id
            WHERE proposal.id = %s
            """,
            (proposal_id,),
        ).fetchone()
        if lineage is None:
            raise RuntimeError(f"proposal {proposal_id} has no Product Brief lineage")
        product_brief_id, stored_contract, backlog = lineage
        try:
            requirement_contract = validate_requirement_contract(stored_contract, int(product_brief_id))
        except ValueError as error:
            requirement_contract = None
            evidence["completion_blocked"] = f"Product Brief requirement contract is invalid: {error}"
        acceptance = evidence.get("acceptance") if isinstance(evidence, dict) else None
        acceptance_evidence = acceptance.get("evidence") if isinstance(acceptance, dict) else None
        if requirement_contract is not None and isinstance(acceptance_evidence, list):
            updated_contract = apply_acceptance_evidence(requirement_contract, acceptance_evidence)
            status_by_source_id = {
                item["source_id"]: item["status"]
                for item in updated_contract["items"]
            }
            updated_backlog = [
                {
                    **item,
                    "status": status_by_source_id.get(str(item.get("id")), item.get("status")),
                }
                for item in backlog
            ]
            connection.execute(
                """
                UPDATE agent_product_briefs
                SET requirements = %s::jsonb, backlog = %s::jsonb, updated_at = %s
                WHERE id = %s
                """,
                (json.dumps(updated_contract), json.dumps(updated_backlog), now(), product_brief_id),
            )
            evidence["product_requirements"] = updated_contract
        if status == "completed" and not (
            requirement_contract is not None
            and isinstance(acceptance_evidence, list)
            and acceptance_evidence_passed(acceptance_evidence, requirement_contract)
        ):
            status = "failed"
            evidence["completion_blocked"] = evidence.get(
                "completion_blocked",
                "Product Brief acceptance evidence is incomplete or failed",
            )
        connection.execute(
            """
            UPDATE agent_deployment_runs
            SET status = %s, evidence = %s::jsonb, completed_at = %s
            WHERE approval_id = %s
            """,
            (status, json.dumps(evidence), now(), approval_id),
        )
        if status == "completed":
            connection.execute(
                """
                UPDATE agent_change_proposals
                SET status = 'completed', updated_at = %s
                WHERE id = %s AND status = 'approved'
                """,
                (now(), proposal_id),
            )
            reconcile_workflow_completion(connection, proposal_id)
        connection.execute(
            """
            INSERT INTO agent_audit_events
                (agent_name, event_type, actor, subject, details, occurred_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                "aicorp-deployment-worker",
                "deployment_completed" if status == "completed" else "deployment_failed",
                "aicorp-deployment-worker",
                str(approval_id),
                json.dumps(evidence),
                now(),
            ),
        )
        connection.commit()


def reconcile_existing_completed_runs() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        proposal_ids = connection.execute(
            """
            SELECT proposal_id
            FROM agent_deployment_runs
            WHERE status = 'completed'
            ORDER BY completed_at, proposal_id
            """
        ).fetchall()
        for (proposal_id,) in proposal_ids:
            reconcile_workflow_completion(connection, proposal_id)
        connection.commit()


def recover_interrupted_runs() -> list[tuple[int, int]]:
    """Release runs left running when the worker was restarted or killed."""
    with psycopg.connect(DATABASE_URL) as connection:
        stale_runs = connection.execute(
            """
            UPDATE agent_deployment_runs
            SET status = 'failed',
                evidence = %s::jsonb,
                completed_at = %s
            WHERE status = 'running'
              AND started_at < %s
            RETURNING approval_id, proposal_id
            """,
            (
                json.dumps({
                    "error": "deployment worker interrupted before completion",
                    "retryable": True,
                }),
                now(),
                now() - timedelta(seconds=STALE_RUN_SECONDS),
            ),
        ).fetchall()
        for approval_id, proposal_id in stale_runs:
            connection.execute(
                """
                INSERT INTO agent_audit_events
                    (agent_name, event_type, actor, subject, details, occurred_at)
                VALUES (%s, 'deployment_failed', %s, %s, %s::jsonb, %s)
                """,
                (
                    "aicorp-deployment-worker",
                    "aicorp-deployment-worker",
                    str(approval_id),
                    json.dumps({"proposal_id": proposal_id, "retryable": True, "reason": "worker_restart"}),
                    now(),
                ),
            )
        connection.commit()
        return [(int(approval_id), int(proposal_id)) for approval_id, proposal_id in stale_runs]


def reconcile_failed_deployment_remediations() -> None:
    """Retry failed-run handoffs that were missed during an agent or worker outage."""
    with psycopg.connect(DATABASE_URL) as connection:
        failed_runs = connection.execute(
            """
            SELECT run.approval_id, run.proposal_id
            FROM agent_deployment_runs AS run
            JOIN agent_approval_requests AS source_approval
                ON source_approval.id = run.approval_id
            WHERE run.status = 'failed'
              AND NOT EXISTS (
                  SELECT 1
                  FROM agent_approval_requests AS remediation
                  WHERE remediation.action = 'start_software_engineer_execution'
                    AND remediation.status IN ('pending', 'approved')
                    AND remediation.context->>'deployment_remediation_for_approval_id'
                        = run.approval_id::text
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM agent_deployment_runs AS newer
                  WHERE newer.proposal_id = run.proposal_id
                    AND newer.status = 'failed'
                    AND newer.started_at > run.started_at
              )
                            AND NOT EXISTS (
                                    SELECT 1
                                    FROM agent_audit_events AS exhausted
                                    WHERE exhausted.event_type = 'deployment_remediation_exhausted'
                                        AND exhausted.subject = run.approval_id::text
                            )
            ORDER BY run.started_at
            """
        ).fetchall()
    for approval_id, proposal_id in failed_runs:
        handoff_failed_deployment(int(approval_id), int(proposal_id))


def main() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        ensure_tables(connection)
    for approval_id, proposal_id in recover_interrupted_runs():
        handoff_failed_deployment(approval_id, proposal_id)
    reconcile_failed_deployment_remediations()
    reconcile_existing_completed_runs()
    while True:
        claimed = claim_request()
        if claimed is not None:
            approval_id, proposal_id = claimed
            try:
                evidence = deploy(approval_id, proposal_id)
                record_result(approval_id, proposal_id, "completed", evidence)
            except DeploymentAcceptanceError as error:
                evidence = {
                    **error.evidence,
                    "error": type(error).__name__,
                    "message": str(error),
                }
                record_result(approval_id, proposal_id, "failed", evidence)
                handoff_failed_deployment(approval_id, proposal_id)
            except Exception as error:
                evidence = {"error": type(error).__name__, "message": str(error)}
                record_result(approval_id, proposal_id, "failed", evidence)
                handoff_failed_deployment(approval_id, proposal_id)
        reconcile_failed_deployment_remediations()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
