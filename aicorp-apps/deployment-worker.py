#!/usr/bin/env python3
"""Execute one approved, bounded AICorp deployment request at a time."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

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


def claim_request() -> tuple[int, int] | None:
    with psycopg.connect(DATABASE_URL) as connection:
        ensure_tables(connection)
        row = connection.execute(
            """
            SELECT approval.id, (approval.context->>'proposal_id')::bigint
            FROM agent_approval_requests AS approval
            JOIN agent_change_proposals AS proposal
              ON proposal.id = (approval.context->>'proposal_id')::bigint
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
            WHERE approval.action = 'deploy'
              AND approval.status = 'approved'
              AND proposal.status = 'approved'
                            AND brief.status <> 'archived'
              AND NOT EXISTS (
                  SELECT 1 FROM agent_deployment_runs AS run
                  WHERE run.approval_id = approval.id
                    AND run.status IN ('running', 'completed')
              )
            ORDER BY approval.id
            FOR UPDATE OF approval, proposal SKIP LOCKED
            LIMIT 1
            """,
        ).fetchone()
        if row is None:
            return None
        approval_id, proposal_id = row
        inserted = connection.execute(
            """
            INSERT INTO agent_deployment_runs
                (approval_id, proposal_id, status, started_at)
            VALUES (%s, %s, 'running', %s)
            ON CONFLICT (approval_id) DO UPDATE
                SET status = 'running', evidence = '{}'::jsonb,
                    started_at = EXCLUDED.started_at, completed_at = NULL
                WHERE agent_deployment_runs.status = 'failed'
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


def deploy(approval_id: int, proposal_id: int) -> dict[str, object]:
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
        return {"approval_id": approval_id, "proposal_id": proposal_id, "checks": checks, "targets": surfaces, "result": "deployed"}
    except Exception:
        for target, backup in backups:
            shutil.copy2(backup, target)
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
          AND NOT EXISTS (
              SELECT 1 FROM agent_execution_tasks AS task
              WHERE task.worker_plan_id = worker.id AND task.status <> 'completed'
          )
        """,
        (completed_at, engineering_plan_id),
    )
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
        connection.execute(
            """
            UPDATE agent_deployment_runs
            SET status = %s, evidence = %s::jsonb, completed_at = %s
            WHERE approval_id = %s
            """,
            (status, json.dumps(evidence), now(), approval_id),
        )
        if status == "completed":
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


def recover_interrupted_runs() -> None:
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


def main() -> None:
    recover_interrupted_runs()
    reconcile_existing_completed_runs()
    while True:
        claimed = claim_request()
        if claimed is not None:
            approval_id, proposal_id = claimed
            try:
                evidence = deploy(approval_id, proposal_id)
                record_result(approval_id, proposal_id, "completed", evidence)
            except Exception as error:
                record_result(approval_id, proposal_id, "failed", {"error": type(error).__name__, "message": str(error)})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
