import ast
import json
import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit


AGENT_DIRECTORY = Path(__file__).parents[1]
AGENT_SOURCE = AGENT_DIRECTORY / "agent.py"
sys.path.insert(0, str(AGENT_DIRECTORY))

import agent as agent_module
import acceptance as acceptance_module
import workers as workers_module
from workers import build_qa_worker_plan
from acceptance import run_product_acceptance
from requirements import (
    attach_requirement_contract,
    apply_acceptance_evidence,
    acceptance_evidence_passed,
    build_requirement_contract,
    require_complete_requirement_contract,
)


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_source = AGENT_SOURCE.read_text(encoding="utf-8")
        cls.governance_source = (AGENT_DIRECTORY / "governance.py").read_text(encoding="utf-8")

    def test_qa_plan_is_deterministic_and_bounded(self):
        engineering_plan = {
            "workstreams": [
                {"id": "WS-01", "acceptance_criteria": ["Health endpoint responds"]},
                {"id": "WS-02", "acceptance_criteria": ["Audit history is preserved"]},
            ],
            "test_strategy": ["Run focused regression checks"],
            "risks_and_open_decisions": ["Evidence may be incomplete"],
            "requirement_contract": build_requirement_contract(
                17,
                {"goals": ["Centralized infrastructure visibility"]},
                [
                    {
                        "id": "1",
                        "title": "Core Dashboard MVP",
                        "description": "Show discovered devices and status.",
                        "priority": "now",
                        "acceptance_criteria": ["The dashboard shows last-known status."],
                        "dependencies": [],
                        "goal_ids": ["GOAL-01"],
                        "status": "ready",
                    }
                ],
            ),
        }
        first = build_qa_worker_plan(engineering_plan)
        second = build_qa_worker_plan(engineering_plan)
        self.assertEqual(first, second)
        self.assertEqual(first["role"], "qa_engineer")
        self.assertEqual(first["files_or_surfaces"], ["workstream:WS-01", "workstream:WS-02"])
        self.assertLessEqual(len(first["tests"]), 16)
        self.assertLessEqual(len(first["risks"]), 16)

    def test_downstream_artifact_cannot_omit_product_requirements(self):
        contract = build_requirement_contract(
            17,
            {"goals": ["Centralized infrastructure visibility"]},
            [
                {
                    "id": "1",
                    "title": "Core Dashboard MVP",
                    "description": "Show discovered devices and status.",
                    "priority": "now",
                    "acceptance_criteria": ["The dashboard shows last-known status."],
                    "dependencies": [],
                    "goal_ids": ["GOAL-01"],
                    "status": "ready",
                }
            ],
        )
        complete = attach_requirement_contract({"name": "technical-plan"}, contract)
        self.assertEqual(require_complete_requirement_contract(complete, contract)["name"], "technical-plan")
        with self.assertRaises(ValueError):
            require_complete_requirement_contract({"name": "incomplete"}, contract)
        with self.assertRaises(ValueError):
            require_complete_requirement_contract(
                {"requirement_contract": {**contract, "items": []}},
                contract,
            )

    def test_unmapped_product_goal_blocks_acceptance(self):
        contract = build_requirement_contract(
            17,
            {"goals": ["Visibility", "Maintenance automation"]},
            [
                {
                    "id": "dashboard",
                    "title": "Core Dashboard MVP",
                    "description": "Show discovered devices and status.",
                    "priority": "now",
                    "acceptance_criteria": ["The dashboard loads within two seconds."],
                    "dependencies": [],
                    "goal_ids": ["GOAL-01"],
                    "status": "ready",
                }
            ],
        )
        evidence = [{
            "acceptance_criterion_id": contract["items"][0]["acceptance_criteria"][0]["id"],
            "result": "passed",
            "command": "GET /product-data",
            "expected": "HTTP 200 under 2000ms",
            "actual": "HTTP 200 in 12ms",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "runtime": {"service": "aicorp-agent", "version": "0.3.0", "source_hash": "abc"},
        }]
        updated = apply_acceptance_evidence(contract, evidence)
        self.assertFalse(acceptance_evidence_passed(evidence, contract))
        self.assertEqual(updated["goals"][0]["status"], "achieved")
        self.assertEqual(updated["goals"][1]["status"], "blocked")

    def test_product_acceptance_emits_contract_evidence(self):
        contract = build_requirement_contract(
            17,
            {"goals": ["Visibility"]},
            [
                {
                    "id": "dashboard",
                    "title": "Core Dashboard MVP",
                    "description": "Show discovered devices and status.",
                    "priority": "now",
                    "acceptance_criteria": ["The dashboard loads within two seconds."],
                    "dependencies": [],
                    "goal_ids": ["GOAL-01"],
                    "status": "ready",
                },
                {
                    "id": "discovery",
                    "title": "Device Discovery Agent",
                    "description": "Detect standard home-server hardware.",
                    "priority": "now",
                    "acceptance_criteria": ["The agent detects standard home-server hardware."],
                    "dependencies": [],
                    "goal_ids": ["GOAL-01"],
                    "status": "ready",
                },
                {
                    "id": "alerting",
                    "title": "Basic Alerting System",
                    "description": "Trigger thresholds and deliver notifications.",
                    "priority": "now",
                    "acceptance_criteria": ["Alerts trigger on thresholds and deliver notifications."],
                    "dependencies": [],
                    "goal_ids": ["GOAL-01"],
                    "status": "ready",
                },
            ],
        )
        device = {"device_id": "control01", "name": "Control 01", "hardware": "home server", "status": "online", "last_seen_at": "2026-01-01T00:00:00+00:00"}
        responses = [
            (200, "<html>HomeLabOps</html>", 5.0),
            (200, {"version": "0.3.0", "devices": [device], "notification_channel": "teams", "notification_deliveries": []}, 10.0),
            (401, {"error": "unauthorized"}, 2.0),
            (200, {"device": device}, 3.0),
            (200, {"device": device, "alerts": [{"state": "firing", "notification_status": "queued"}]}, 3.0),
            (200, {"device": device, "alerts": [{"state": "resolved", "notification_status": "queued"}]}, 3.0),
            (200, {"version": "0.3.0", "notification_channel": "teams", "notification_deliveries": [{"device_id": "control01", "event_type": "product_alert_firing", "status": "sent"}]}, 3.0),
            (200, {"devices": [device]}, 3.0),
        ]
        with patch("acceptance._request", side_effect=responses):
            result = run_product_acceptance("http://test", "token", contract, "abc")
        self.assertTrue(result["passed"])
        self.assertTrue(result["contract_passed"])
        self.assertEqual(len(result["evidence"]), 3)
        self.assertTrue(all(item["runtime"]["source_hash"] == "abc" for item in result["evidence"]))

    def test_backup_acceptance_uses_executable_host_checks(self):
        self.assertEqual(
            acceptance_module._criterion_check_id("Automated Backup Script", "Runs on schedule"),
            "backup-schedule",
        )
        self.assertEqual(
            acceptance_module._criterion_check_id("Automated Backup Script", "Logs success/fail"),
            "backup-logging",
        )
        responses = [
            subprocess.CompletedProcess([], 0, stdout="enabled\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="active\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="Backup completed: /mnt/backup/aicorp/2026-09-19\n", stderr=""),
        ]
        with patch.object(acceptance_module.subprocess, "run", side_effect=responses) as run_command:
            checks = acceptance_module._backup_acceptance_checks()

        self.assertEqual([check["check_id"] for check in checks], ["backup-schedule", "backup-logging"])
        self.assertTrue(all(check["passed"] for check in checks))
        self.assertEqual(run_command.call_count, 3)

    def test_deployment_failure_context_preserves_failed_acceptance_details(self):
        evidence = {
            "error": "DeploymentAcceptanceError",
            "message": "deployed runtime failed Product Brief acceptance checks",
            "acceptance": {
                "evidence": [
                    {
                        "acceptance_criterion_id": "PB-6-ITEM-B-002-AC-01",
                        "requirement_id": "PB-6-ITEM-B-002",
                        "result": "failed",
                        "command": "systemctl is-enabled aicorp-backup.timer",
                        "expected": "enabled",
                        "actual": "No executable acceptance check mapping",
                    },
                    {
                        "acceptance_criterion_id": "PB-6-ITEM-B-002-AC-02",
                        "requirement_id": "PB-6-ITEM-B-002",
                        "result": "passed",
                    },
                ]
            },
        }
        context = agent_module.deployment_failure_context(evidence)
        self.assertEqual(context["error"], "DeploymentAcceptanceError")
        self.assertEqual(len(context["acceptance_failures"]), 1)
        self.assertEqual(
            context["acceptance_failures"][0]["acceptance_criterion_id"],
            "PB-6-ITEM-B-002-AC-01",
        )

    def test_work_item_orchestrator_waits_for_predecessor_before_starting(self):
        class FakeResult:
            def fetchall(self):
                return [(41, 51, {"workstreams": [{"id": "WS-02"}]})]

            def fetchone(self):
                return None

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                return FakeResult()

        with patch.object(agent_module.psycopg, "connect", return_value=FakeConnection()), patch.object(
            agent_module, "workstream_dependencies_completed", return_value=False
        ), patch.object(agent_module, "auto_approve_handoff") as auto_handoff:
            agent_module.run_work_item_orchestrator_once()

        auto_handoff.assert_not_called()

    def test_deterministic_execution_scope_failure_is_recorded(self):
        class FakeResult:
            def __init__(self, rows=(), row=None):
                self.rows = list(rows)
                self.row = row

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.row

        class FakeConnection:
            def __init__(self, task_rows=(), updated=False):
                self.task_rows = task_rows
                self.updated = updated
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                self.statements.append((statement, parameters))
                if "SELECT task.id" in statement:
                    return FakeResult(rows=self.task_rows)
                if "UPDATE agent_execution_tasks" in statement:
                    return FakeResult(row=(15,) if self.updated else None)
                raise AssertionError(f"unexpected SQL: {statement}")

            def commit(self):
                pass

        task_connection = FakeConnection(
            task_rows=[(15, "WS-02", {"files_or_surfaces": ["workstream:WS-02"]}, {})]
        )
        failure_connection = FakeConnection(updated=True)
        with patch.object(
            agent_module.psycopg,
            "connect",
            side_effect=[task_connection, failure_connection],
        ), patch.object(agent_module, "submit_execution_evidence") as submit_evidence, patch.object(
            agent_module, "record_audit_event"
        ) as audit:
            agent_module.run_execution_worker_once()

        submit_evidence.assert_not_called()
        update_sql, update_parameters = failure_connection.statements[0]
        self.assertIn("SET status = 'failed'", update_sql)
        self.assertEqual(update_parameters[-1], 15)
        audit.assert_called_once()

    def test_failed_execution_configuration_queues_worker_plan_repair(self):
        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def __init__(self, source=False):
                self.source = source

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                if self.source and "SELECT task.worker_plan_id" in statement:
                    return FakeResult((11, 51, "WS-01", "failed", {"error_type": "ExecutionConfigurationError"}, 378, {}))
                if "SELECT id" in statement and "execution_plan_repair_for_task_id" in statement:
                    return FakeResult()
                if "SELECT count(*)" in statement:
                    return FakeResult((0,))
                raise AssertionError(f"unexpected SQL: {statement}")

            def commit(self):
                pass

        source_connection = FakeConnection(source=True)
        audit_connection = FakeConnection()
        with patch.object(
            agent_module.psycopg,
            "connect",
            side_effect=[source_connection, audit_connection],
        ), patch.object(
            agent_module,
            "queue_generation_approval",
            return_value=901,
        ) as queue_generation, patch.object(agent_module, "record_audit_event") as audit:
            result = agent_module.queue_execution_plan_repair(57)

        self.assertEqual(result, 901)
        queue_generation.assert_called_once_with(
            "generate_software_engineer_plan",
            agent_module.ENGINEERING_MANAGER_NAME,
            {
                "engineering_plan_id": 51,
                "role": "software_engineer",
                "work_item_id": "WS-01",
                "failed_worker_plan_id": 11,
                "execution_plan_repair_for_task_id": 57,
            },
        )
        audit.assert_called_once()

    def test_non_thinking_generation_sends_native_reasoning_controls(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}'

        with patch.object(workers_module.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            result = workers_module._generate(
                "http://litellm:4000",
                "key",
                "local-qwen3.5-9b",
                "system",
                "prompt",
                max_tokens=workers_module.PROPOSAL_MAX_TOKENS,
                think=False,
            )

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(result, {})
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertFalse(payload["think"])
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertFalse(payload["extra_body"]["think"])

    def test_repository_budget_exhaustion_is_terminal(self):
        class FakeResult:
            def fetchone(self):
                return (44,)

            def fetchall(self):
                return []

        class FakeConnection:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                self.statements.append((statement, parameters))
                return FakeResult()

            def commit(self):
                pass

        selection = FakeConnection()
        dependency = FakeConnection()
        failure = FakeConnection()
        error = workers_module.GenerationBudgetExhaustedError("budget exhausted")
        selection.execute = lambda statement, parameters=(): type(
            "Rows",
            (),
            {"fetchall": lambda self: [(44, "WS-01", 7, {}, {})]},
        )()
        with patch.object(agent_module.psycopg, "connect", side_effect=[selection, dependency, failure]), patch.object(
            agent_module, "workstream_dependencies_completed", return_value=True
        ), patch.object(agent_module, "generate_work_item_proposal", side_effect=error), patch.object(
            agent_module, "record_audit_event"
        ) as audit:
            agent_module.run_repository_worker_once()

        update_sql, update_parameters = failure.statements[0]
        self.assertIn("SET status = 'failed'", update_sql)
        self.assertEqual(update_parameters[-1], 44)
        audit.assert_called_once()

    def test_deployment_remediation_reuses_existing_handoff(self):
        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def __init__(self, existing=None):
                self.existing = existing

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                if "FROM agent_deployment_runs AS source_run" in statement:
                    return FakeResult((
                        "deploy", "approved", {"planning_generation": 4}, 77, "failed",
                        {"acceptance": {"evidence": [{"result": "failed", "actual": "broken"}]}},
                        "approved", "approved", "WS-01", 7, "software_engineer", 4, 6,
                    ))
                if "FROM agent_deployment_runs AS failed_run" in statement:
                    return FakeResult((1,))
                if "deployment_remediation_for_approval_id" in statement:
                    return FakeResult(self.existing)
                return FakeResult()

            def commit(self):
                pass

        context = {}
        with patch.object(agent_module.psycopg, "connect", side_effect=[FakeConnection(), FakeConnection(), FakeConnection((901,)), FakeConnection()]), patch.object(
            agent_module, "planning_generation_matches", return_value=True
        ), patch.object(agent_module, "auto_approve_handoff") as auto_handoff, patch.object(
            agent_module, "start_automated_execution", return_value=902
        ), patch.object(agent_module, "record_audit_event") as audit:
            result = agent_module.queue_deployment_remediation(100, 77)

        self.assertEqual(result["execution_approval_id"], 901)
        self.assertEqual(result["execution_task_id"], 902)
        auto_handoff.assert_not_called()
        audit.assert_called_once()

    def test_deployment_remediation_exhausts_after_max_attempts(self):
        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def __init__(self, source=False):
                self.source = source

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                if self.source:
                    return FakeResult((
                        "deploy", "approved", {"planning_generation": 4}, 77, "failed",
                        {"acceptance": {"evidence": [{"result": "failed", "actual": "unmapped"}]}},
                        "approved", "approved", "WS-01", 7, "software_engineer", 4, 6,
                    ))
                if "FROM agent_deployment_runs AS failed_run" in statement:
                    return FakeResult((agent_module.MAX_DEPLOYMENT_ATTEMPTS,))
                if "deployment_remediation_exhausted" in statement:
                    return FakeResult()
                if "UPDATE agent_deployment_runs" in statement and "evidence" in statement:
                    return FakeResult()
                raise AssertionError(f"unexpected SQL: {statement}")

            def commit(self):
                pass

        with patch.object(
            agent_module.psycopg,
            "connect",
            side_effect=[FakeConnection(source=True), FakeConnection()],
        ), patch.object(agent_module, "planning_generation_matches", return_value=True), patch.object(
            agent_module, "auto_approve_handoff"
        ) as auto_handoff, patch.object(agent_module, "record_audit_event") as audit:
            result = agent_module.queue_deployment_remediation(100, 77)

        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["failed_attempts"], agent_module.MAX_DEPLOYMENT_ATTEMPTS)
        auto_handoff.assert_not_called()
        audit.assert_called_once()

    def test_break_deployment_retry_loop_cancels_remediation_lineage(self):
        class FakeResult:
            def __init__(self, rows=(), row=None):
                self.rows = list(rows)
                self.row = row

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                if "SELECT source_approval.status" in statement:
                    return FakeResult(row=("approved", "failed", 77, {"planning_generation": 4}, 7, "WS-01"))
                if "SELECT remediation_approval.id" in statement:
                    return FakeResult(rows=[(401, 501), (402, 502)])
                if "FROM agent_change_proposals" in statement and "execution_task_id = ANY" in statement:
                    return FakeResult(rows=[(601,), (602,)])
                if "SELECT approval.id, deployment.status" in statement:
                    return FakeResult(rows=[(701, None), (702, "failed")])
                if "UPDATE agent_approval_requests" in statement:
                    return FakeResult(rows=[(401,), (402,), (701,), (702,)])
                if "UPDATE agent_change_proposals" in statement:
                    return FakeResult(rows=[(601,), (602,)])
                if "UPDATE agent_execution_tasks" in statement:
                    return FakeResult(rows=[(501,), (502,)])
                if "UPDATE agent_deployment_runs" in statement:
                    return FakeResult()
                raise AssertionError(f"unexpected SQL: {statement}")

            def commit(self):
                pass

        with patch.object(agent_module.psycopg, "connect", return_value=FakeConnection()), patch.object(
            agent_module, "planning_generation_matches", return_value=True
        ), patch.object(agent_module, "record_audit_event") as audit:
            result = agent_module.break_deployment_retry_loop(100, "andrew", "stop repeated acceptance failures")

        self.assertEqual(result["status"], "broken")
        self.assertEqual(result["cancelled_approval_ids"], [401, 402, 701, 702])
        self.assertEqual(result["superseded_task_ids"], [501, 502])
        self.assertEqual(result["superseded_proposal_ids"], [601, 602])
        self.assertEqual(audit.call_count, 2)

    def test_deployment_retry_accepts_only_linked_replacement_proposal(self):
        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def __init__(self, target):
                self.target = target

            def execute(self, statement, parameters=()):
                if "FROM agent_deployment_runs AS source_run" in statement:
                    return FakeResult((
                        "deploy", "approved", {"planning_generation": 4}, 77, "failed",
                        "approved", "approved",
                    ))
                if "FROM agent_change_proposals AS target" in statement:
                    return FakeResult(self.target)
                if "SELECT retry.id" in statement:
                    return FakeResult()
                return FakeResult()

            def commit(self):
                pass

        target = (
            "approved", "WS-01", 7, "software_engineer", 6,
            {
                "deployment_remediation_for_approval_id": 100,
                "deployment_remediation_for_proposal_id": 77,
            },
            "WS-01", 7, 6,
        )
        stamped_context = {
            "proposal_id": 88,
            "source_proposal_id": 77,
            "source_deployment_approval_id": 100,
            "planning_generation": 4,
        }
        with patch.object(agent_module, "planning_generation_matches", return_value=True), patch.object(
            agent_module, "add_planning_generation", return_value=stamped_context
        ), patch.object(agent_module, "create_approval_request", return_value=903) as create_request, patch.object(
            agent_module, "decide_approval"
        ) as decide_request, patch.object(agent_module, "record_audit_event"):
            request_id = agent_module.create_deployment_retry_approval(
                FakeConnection(target),
                "aicorp-qa-engineer",
                100,
                "Retry the corrected deployment after QA approval.",
                replacement_proposal_id=88,
            )

        self.assertEqual(request_id, 903)
        self.assertEqual(create_request.call_args.args[5], stamped_context)
        self.assertEqual(
            decide_request.call_args.args[1:5],
            (903, "approved", "aicorp-qa-engineer", "Automatically approved by the deployment retry policy."),
        )

    def test_deployment_retry_rejects_unlinked_replacement_proposal(self):
        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, statement, parameters=()):
                if "FROM agent_deployment_runs AS source_run" in statement:
                    return FakeResult((
                        "deploy", "approved", {"planning_generation": 4}, 77, "failed",
                        "approved", "approved",
                    ))
                if "FROM agent_change_proposals AS target" in statement:
                    return FakeResult((
                        "approved", "WS-01", 7, "software_engineer", 6,
                        {"deployment_remediation_for_approval_id": 999},
                        "WS-01", 7, 6,
                    ))
                raise AssertionError(f"unexpected SQL: {statement}")

        with self.assertRaises(agent_module.DeploymentRetryConflict), patch.object(
            agent_module, "planning_generation_matches", return_value=True
        ), patch.object(agent_module, "create_approval_request") as create_request:
            agent_module.create_deployment_retry_approval(
                FakeConnection(),
                "aicorp-qa-engineer",
                100,
                "Retry the corrected deployment after QA approval.",
                replacement_proposal_id=88,
            )
        create_request.assert_not_called()

    def test_native_ollama_base_url_removes_only_trailing_v1(self):
        tree = ast.parse(self.agent_source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "native_ollama_base_url"
        )
        namespace = {"urlsplit": urlsplit}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(AGENT_SOURCE), "exec"), namespace)
        normalize = namespace["native_ollama_base_url"]
        self.assertEqual(normalize("http://ollama:11434/v1"), "http://ollama:11434")
        self.assertEqual(normalize("http://ollama:11434/v1/"), "http://ollama:11434")
        self.assertEqual(normalize("http://ollama:11434"), "http://ollama:11434")

    def test_workflow_guards_remain_present(self):
        self.assertGreaterEqual(
            self.agent_source.count("ON CONFLICT (approval_request_id) DO NOTHING"),
            4,
        )
        self.assertGreaterEqual(self.agent_source.count("pg_advisory_xact_lock"), 2)
        self.assertIn("generation_budget_exhausted", self.agent_source)
        self.assertIn("QA approval context does not match this execution task", self.agent_source)
        self.assertIn("QA approval context does not match this proposal", self.agent_source)
        self.assertIn("qa.engineering_plan_id", self.agent_source)
        deployment_source = (AGENT_DIRECTORY.parent / "deployment-worker.py").read_text(encoding="utf-8")
        self.assertIn("JOIN agent_planning_state AS planning_state", deployment_source)
        self.assertIn("approval.context->>'planning_generation'", deployment_source)
        self.assertIn("FOR UPDATE OF planning_state, approval, proposal SKIP LOCKED", deployment_source)

    def test_failed_deployments_are_terminal_and_retries_are_separate(self):
        deployment_source = (AGENT_DIRECTORY.parent / "deployment-worker.py").read_text(encoding="utf-8")
        self.assertIn("approval.action IN ('deploy', 'retry_deployment')", deployment_source)
        self.assertIn("approval.action = 'retry_deployment'", deployment_source)
        self.assertIn("source_run.status = 'failed'", deployment_source)
        self.assertIn("AND NOT EXISTS (", deployment_source)
        self.assertIn("ON CONFLICT (approval_id) DO NOTHING", deployment_source)
        self.assertNotIn("WHERE agent_deployment_runs.status = 'failed'", deployment_source)
        self.assertIn("/deployment-runs/", self.agent_source)
        self.assertIn("/deployment-failures/remediate", self.agent_source)
        self.assertIn("deployment_remediation_for_approval_id", self.agent_source)
        self.assertIn("reconcile_failed_deployment_remediations", deployment_source)
        self.assertIn("source_proposal_id", deployment_source)
        self.assertIn("DEPLOYMENT_RETRY_ACTION", self.governance_source)

    def test_deployment_retry_requires_failed_run_and_stamps_generation(self):
        class FakeResult:
            def __init__(self, row=None, rowcount=0):
                self.row = row
                self.rowcount = rowcount

            def fetchone(self):
                return self.row

        class FakeConnection:
            def __init__(self, source_row, existing_row=None):
                self.source_row = source_row
                self.existing_row = existing_row
                self.statements = []

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                if "SELECT retry.id" in statement:
                    return FakeResult(self.existing_row)
                if "FROM agent_deployment_runs AS source_run" in statement:
                    return FakeResult(self.source_row)
                if "UPDATE agent_approval_requests" in statement:
                    return FakeResult(rowcount=1)
                raise AssertionError(f"unexpected SQL in retry test: {statement}")

            def commit(self):
                pass

        source_row = (
            "deploy",
            "approved",
            {"planning_generation": 4},
            77,
            "failed",
            "approved",
            "approved",
        )
        connection = FakeConnection(source_row)
        stamped_context = {
            "proposal_id": 77,
            "source_deployment_approval_id": 100,
            "planning_generation": 4,
        }
        with patch.object(agent_module, "planning_generation_matches", return_value=True), patch.object(
            agent_module, "add_planning_generation", return_value=stamped_context
        ), patch.object(agent_module, "create_approval_request", return_value=901) as create_request, patch.object(
            agent_module, "record_audit_event"
        ):
            request_id = agent_module.create_deployment_retry_approval(
                connection,
                "andrew",
                100,
                "Retry after correcting the deployment failure.",
            )

        self.assertEqual(request_id, 901)
        self.assertEqual(create_request.call_args.args[2], agent_module.DEPLOYMENT_RETRY_ACTION)
        self.assertEqual(create_request.call_args.args[5], stamped_context)

    def test_deployment_retry_rejects_completed_runs_and_duplicate_pending_retry(self):
        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def __init__(self, source_row, existing_row=None):
                self.source_row = source_row
                self.existing_row = existing_row

            def execute(self, statement, parameters=()):
                if "SELECT retry.id" in statement:
                    return FakeResult(self.existing_row)
                if "FROM agent_deployment_runs AS source_run" in statement:
                    return FakeResult(self.source_row)
                raise AssertionError(f"unexpected SQL in retry conflict test: {statement}")

            def commit(self):
                pass

        completed_source = (
            "deploy",
            "approved",
            {"planning_generation": 4},
            77,
            "completed",
            "approved",
            "approved",
        )
        with self.assertRaises(agent_module.DeploymentRetryConflict), patch.object(
            agent_module, "create_approval_request"
        ) as create_request:
            agent_module.create_deployment_retry_approval(
                FakeConnection(completed_source),
                "andrew",
                100,
                "Retry after correcting the deployment failure.",
            )
        create_request.assert_not_called()

        failed_source = (*completed_source[:4], "failed", *completed_source[5:])
        with self.assertRaises(agent_module.DeploymentRetryConflict), patch.object(
            agent_module, "planning_generation_matches", return_value=True
        ), patch.object(agent_module, "create_approval_request") as create_request:
            agent_module.create_deployment_retry_approval(
                FakeConnection(failed_source, (902, "pending", "running")),
                "andrew",
                100,
                "Retry after correcting the deployment failure.",
            )
        create_request.assert_not_called()

    def test_generation_approval_returns_stamped_id(self):
        class FakeResult:
            def fetchone(self):
                return None

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement, parameters=()):
                return FakeResult()

            def commit(self):
                pass

        context = {"product_context": "HomeLabOps"}

        def stamped_context(connection, action, original):
            return {**original, "planning_generation": 7}

        with patch.object(agent_module.psycopg, "connect", return_value=FakeConnection()), patch.object(
            agent_module, "add_planning_generation", side_effect=stamped_context
        ), patch.object(agent_module, "create_approval_request", return_value=123) as create_request, patch.object(
            agent_module, "decide_approval"
        ), patch.object(agent_module, "record_audit_event"), patch.object(agent_module.threading, "Thread"):
            request_id = agent_module.queue_generation_approval(
                "generate_product_brief",
                agent_module.PRODUCT_MANAGER_NAME,
                context,
            )

        self.assertEqual(request_id, 123)
        approval_context = create_request.call_args.args[5]
        self.assertEqual(approval_context["planning_generation"], 7)

    def test_reset_scope_includes_current_approvals_without_active_briefs(self):
        class FakeResult:
            def __init__(self, rows=()):
                self.rows = list(rows)

            def fetchall(self):
                return self.rows

        class FakeConnection:
            def __init__(self):
                self.statements = []

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                if "FROM agent_approval_requests" in statement:
                    return FakeResult([
                        (301, "generate_product_brief", "approved"),
                        (402, "deploy", "approved"),
                    ])
                if "FROM agent_product_briefs" in statement:
                    return FakeResult([])
                if "FROM agent_deployment_runs" in statement:
                    return FakeResult([(402, 77, "completed")])
                if "FROM agent_technical_plans" in statement:
                    return FakeResult([])
                if "FROM agent_engineering_plans" in statement:
                    return FakeResult([])
                if "FROM agent_worker_plans" in statement:
                    return FakeResult([])
                if "FROM agent_execution_tasks" in statement:
                    return FakeResult([])
                if "FROM agent_change_proposals" in statement:
                    return FakeResult([])
                raise AssertionError(f"unexpected SQL in scope test: {statement}")

        connection = FakeConnection()
        with patch.object(agent_module, "current_planning_generation", return_value=4):
            scope = agent_module.planning_reset_scope(connection)

        self.assertEqual(scope["active_product_brief_ids"], [])
        self.assertEqual(scope["approval_ids"], [301, 402])
        self.assertEqual(scope["cancelable_approval_ids"], [301])
        self.assertEqual(scope["completed_deployment_approval_ids"], [402])
        self.assertEqual(scope["next_planning_generation"], 5)
        scope_sql = "\n".join(connection.statements)
        self.assertIn("approval.context->>'planning_generation' = %s::text", scope_sql)
        self.assertIn("context->>'planning_generation' = %s::text", scope_sql)

    def test_planning_reset_requires_explicit_confirmation_and_generation_match(self):
        self.assertTrue(agent_module.planning_reset_confirmed({"confirm": True}))
        self.assertTrue(agent_module.planning_reset_confirmed({"confirmation": "RESET_PLANNING_WORKSPACE"}))
        self.assertFalse(agent_module.planning_reset_confirmed({"confirm": False}))
        self.assertFalse(agent_module.planning_reset_confirmed({"confirmation": "reset"}))
        self.assertTrue(agent_module.planning_generation_matches(None, {}, generation=1))
        self.assertFalse(agent_module.planning_generation_matches(None, {}, generation=2))
        self.assertTrue(agent_module.planning_generation_matches(None, {"planning_generation": "4"}, generation=4))
        self.assertFalse(agent_module.planning_generation_matches(None, {"planning_generation": "old"}, generation=4))
        self.assertIn("reset_planning_workspace", self.governance_source)
        self.assertIn("agent_planning_state", self.governance_source)
        self.assertIn("/planning-reset/preview", self.agent_source)
        self.assertIn("/planning-reset/request", self.agent_source)

    def test_planning_reset_transaction_preserves_completed_deployments(self):
        class FakeResult:
            def __init__(self, rows=()):
                self.rows = list(rows)

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.rows[0] if self.rows else None

        class FakeConnection:
            def __init__(self):
                self.statements = []

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                if "UPDATE agent_product_briefs" in statement:
                    return FakeResult([(11,)])
                if "UPDATE agent_technical_plans" in statement:
                    return FakeResult([(21,)])
                if "UPDATE agent_engineering_plans" in statement:
                    return FakeResult([(31,)])
                if "UPDATE agent_worker_plans" in statement:
                    return FakeResult([(41,)])
                if "UPDATE agent_execution_tasks" in statement:
                    return FakeResult([(51,)])
                if "UPDATE agent_change_proposals" in statement:
                    return FakeResult([(61,)])
                if "SELECT id, action, status" in statement:
                    return FakeResult([(101, "generate_product_brief", "approved"), (102, "deploy", "approved")])
                if "UPDATE agent_approval_requests" in statement:
                    return FakeResult([(101,)])
                if "UPDATE agent_planning_state" in statement:
                    return FakeResult([(4,)])
                raise AssertionError(f"unexpected SQL in reset test: {statement}")

        scope = {
            "planning_generation": 3,
            "active_product_brief_ids": [11],
            "technical_plan_ids": [21],
            "engineering_plan_ids": [31],
            "worker_plan_ids": [41],
            "execution_task_ids": [51],
            "change_proposal_ids": [61],
            "running_deployment_ids": [],
            "completed_deployment_approval_ids": [102],
        }
        connection = FakeConnection()
        with patch.object(agent_module, "current_planning_generation", return_value=3), patch.object(
            agent_module, "planning_reset_scope", return_value=scope
        ), patch.object(agent_module, "require_current_planning_approval"), patch.object(
            agent_module, "record_audit_event"
        ):
            result = agent_module.reset_planning_workspace_in_connection(
                connection,
                "andrew",
                "Start the planning workspace over.",
                True,
                100,
            )
        self.assertEqual(result["archived_product_brief_ids"], [11])
        self.assertEqual(result["cancelled_approval_ids"], [101])
        self.assertEqual(result["preserved_completed_deployment_approval_ids"], [102])
        self.assertEqual(result["planning_generation"], 4)
        self.assertIn("SET status = 'superseded'", "\n".join(connection.statements))
        self.assertIn("status NOT IN ('completed', 'superseded')", "\n".join(connection.statements))
        self.assertNotIn("DELETE FROM", "\n".join(connection.statements))

    def test_planning_reset_blocks_active_deployment_and_missing_confirmation(self):
        class FakeConnection:
            def execute(self, statement, parameters=()):
                raise AssertionError("the reset should validate confirmation before database mutation")

        with self.assertRaises(ValueError):
            agent_module.reset_planning_workspace_in_connection(
                FakeConnection(), "andrew", "reset", False, 100,
            )

        scope = {
            "planning_generation": 3,
            "active_product_brief_ids": [11],
            "running_deployment_ids": [999],
        }
        connection = FakeConnection()
        with patch.object(agent_module, "current_planning_generation", return_value=3), patch.object(
            agent_module, "planning_reset_scope", return_value=scope
        ), patch.object(agent_module, "require_current_planning_approval"):
            with self.assertRaises(agent_module.PlanningResetConflict):
                agent_module.reset_planning_workspace_in_connection(
                    connection, "andrew", "reset", True, 100,
                )

    def test_planning_reset_rechecks_approval_before_mutation(self):
        class FakeConnection:
            def __init__(self):
                self.statements = []

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                raise AssertionError("stale reset approval should stop before SQL mutation")

        connection = FakeConnection()
        with patch.object(
            agent_module,
            "require_current_planning_approval",
            side_effect=agent_module.PlanningResetConflict("planning reset invalidated this approval request"),
        ):
            with self.assertRaises(agent_module.PlanningResetConflict):
                agent_module.reset_planning_workspace_in_connection(
                    connection, "andrew", "reset", True, 100,
                )

        self.assertEqual(connection.statements, [])


if __name__ == "__main__":
    unittest.main()
