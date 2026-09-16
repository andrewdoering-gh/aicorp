# AICorp Platform Stack

This Compose project provides the initial AICorp platform services on `aicorp-control01`:

- PostgreSQL for relational state.
- Redis for durable queue and coordination state.
- Qdrant for vector retrieval.
- LiteLLM as the model gateway.
- Open WebUI as the initial human interface.
- Prometheus for initial host metrics.
- Alertmanager for webhook notification delivery.
- A single persistent health-summary agent.
- Ollama with a small local model as LiteLLM fallback.

The stack uses bind mounts under `/opt/aicorp` and keeps all service ports bound to localhost by default. It does not contain credentials or application data.

Prometheus scrapes Node Exporter on `aicorp-control01` through the Docker host
gateway. Install Node Exporter before starting or validating Prometheus.

## Prerequisites

1. Apply the AICorp Ansible control-plane playbook so Docker, Compose v2, and the persistent directories are present.
2. Apply the AICorp Node Exporter playbook.
3. Install the `community.general` and `community.docker` Ansible collections.
3. Copy the required values from `.env.example` into the Admin VM's protected external environment. Do not commit a populated `.env` file.
4. Confirm that the hosted model provider and paid-usage limits have human approval.

Required external variables:

```text
POSTGRES_PASSWORD
DATABASE_URL
LITELLM_MASTER_KEY
OPENAI_API_KEY
GITHUB_TOKEN
OLLAMA_BASE_URL
```

Hosted inference uses the LiteLLM route `gpt-4o-mini`, backed by the OpenAI API
and the protected `OPENAI_API_KEY` value. Keep the key only in
`/opt/aicorp/.env`; never commit it or print it in logs.

`DATABASE_URL` must use the PostgreSQL service hostname `postgres` and the
`aicorp` database user and database. URL-encode any special characters in the
password before placing the value in the protected environment file.

`POSTGRES_DB` and `POSTGRES_USER` default to `aicorp` when omitted.

## Deploy

Run the deployment playbook from the Admin VM. It copies the Compose and
LiteLLM configuration files to `aicorp-control01` and runs Docker Compose on
that host. The external environment file remains on `aicorp-control01`.

From the Admin VM:

```bash
cd ~/git/drewnet/drewnet-config/ansible/aicorp
ansible-galaxy collection install -r requirements.yml
ansible-playbook \
	-i inventory/hosts.yml \
	playbooks/deploy-platform.yml \
	--limit aicorp-control01
```

The playbook requires `/opt/aicorp/.env` to already exist on `aicorp-control01`
with mode `0600`. It never copies or prints that file.

## Inspect

After deployment, connect to `aicorp-control01`:

```bash
ssh drew@control01.home.arpa
cd /opt/aicorp
docker compose config
docker compose up -d
docker compose ps
docker compose logs --tail=100
```

The first startup may take time while images download and databases initialize. The expected host-local interfaces are:

```text
LiteLLM:   http://127.0.0.1:4000
Open WebUI: http://127.0.0.1:3000
Prometheus:  http://127.0.0.1:9090
Alertmanager: http://127.0.0.1:9093
```

Do not expose these ports publicly until authentication, network policy, and the access path have been reviewed.

Alertmanager sends grouped firing and resolved alerts to the external
`ALERTMANAGER_WEBHOOK_URL`. The receiver must accept the Prometheus
Alertmanager webhook payload format. Keep the URL in the protected
`/opt/aicorp/.env` file; it is not committed or copied through Git.

Prometheus also scrapes the agent metrics endpoint. The initial operational
alerts cover agent availability, failed agent runs, and approval requests that
remain pending for more than 30 minutes.

## HomeLabOps Product Prototype

HomeLabOps is the first product prototype built on the AICorp platform. It is
an operator-facing read-only console for quickly understanding the state of a
small homelab. It combines monitored service health, active alerts, and the
latest health-summary report into one focused view.

After deployment, access it through an SSH tunnel to the control plane:

```bash
ssh -L 8081:127.0.0.1:8081 drew@control01.home.arpa
```

Then open:

```text
http://127.0.0.1:8081/product
```

The prototype is deliberately read-only. It does not execute commands, change
infrastructure, restart services, access secrets, or bypass the approval
workflow. Its first product hypothesis is that homelab operators need a calm,
evidence-based operational brief rather than separate monitoring screens and
agent conversations.

## Local Model Fallback

Ollama runs on the desktop outside the AICorp IaC environment. LiteLLM keeps
`gpt-4o-mini` as the default route and falls back to
`local-qwen3.5-9b` through the external `OLLAMA_BASE_URL` when the hosted
route fails. Thinking output is disabled for this route for compatibility with
the pinned LiteLLM version; local inference is CPU-bound and is not a
replacement for hosted model quality.

The local model is available through LiteLLM using the model name
`local-qwen3.5-9b`. The desktop must expose Ollama on a reachable address,
for example `http://desktop-host.example:11434`, and its firewall must allow
the control-plane address. Keep the real desktop URL in the protected
`/opt/aicorp/.env` file.

## Persistent Agent

The initial agent is `aicorp-health-summary`. It runs on a schedule, reads
Prometheus target and alert state, asks LiteLLM for a concise summary, and
stores run status and reports in PostgreSQL. It has no shell, infrastructure,
external messaging, secret-reading, or privileged tools.

The default interval is one hour. Configure it through the protected
environment file when needed:

```text
AGENT_NAME=aicorp-health-summary
AGENT_MODEL=gpt-4o-mini
AGENT_INTERVAL_SECONDS=3600
```

The latest completed report is available through the read-only local endpoint:

```text
http://127.0.0.1:8081/report
```

Access it through an SSH tunnel. The endpoint has no write routes and does not
expose agent tools.

### Open WebUI Report Tool

The repository includes a read-only Open WebUI Tool at
`open-webui/functions/aicorp_health_report.py`. Import it through the Open
WebUI administrator interface, then set its `agent_report_url` valve to:

```text
http://agent:8081/report
```

Enable the tool only for the intended AICorp workspace or user group. The tool
can read the latest completed report but cannot modify infrastructure, execute
commands, access secrets, or send external messages. Remove or disable the
tool if the agent report endpoint is not required.

## Agent Knowledge and Retrieval

Human-approved documents can be ingested into the Qdrant collection
`aicorp-knowledge` using the explicit `knowledge.py ingest` command. The agent
only reads matching chunks during report generation; it cannot ingest or modify
documents.

Embeddings are supplied by the external desktop Ollama endpoint through
`EMBEDDING_MODEL` (default: `nomic-embed-text`). Keep that model installed on
the desktop before ingestion. Ingest reviewed AICorp documents only; do not
ingest arbitrary downloads or unrestricted web content.

The ingestion command is intentionally human-run and is not part of the
scheduled agent loop.

Agent persistence uses the `agent_runs` table in the AICorp PostgreSQL
database. A report is complete only when the run status is `completed`; failed
runs record an error type without storing credentials or connection details.

## Agent Authorization and Audit

The agent runtime has an empty authorized-tool registry by default. The current
health-summary workflow uses only read-only Prometheus, Qdrant, Ollama, LiteLLM,
and PostgreSQL paths. It cannot execute shell commands, change infrastructure,
send external messages, or access secrets.

The runtime creates two governance tables:

```text
agent_audit_events
agent_approval_requests
```

Agent run start, completion, and failure events are recorded in the audit
ledger. Approval requests require an action, requester, reason, decision maker,
and decision reason. The approval API is only a ledger interface and does not
execute approved actions.

The approval API is disabled unless `AGENT_APPROVAL_TOKEN` is configured in the
protected environment. When enabled, it only creates and transitions approval
ledger records; it never executes an approved action. Keep the token out of
Git and use separate protected per-agent tokens for delegated generation
approvals. The allowed chain is:

```text
product_manager       -> generate_technical_plan
cto                   -> generate_engineering_plan
engineering_manager   -> generate_software_engineer_plan, generate_qa_plan
```

The Product Manager brief remains human-approved because there is no upstream
product-governance agent. Agents cannot approve their own requests, publish
artifacts, commit, merge, or deploy. The QA Engineer automatically approves a
proposal after its repository review passes; deployment remains human-approved.

Approved generation requests automatically dispatch the responsible agent's
next generation route. Dispatch failures are audited for operator retry; the
system never substitutes a different action. Human-only publication and
deployment remain ledger gates. QA-owned repository approval is automatic after
proposal review and does not auto-apply, commit, or deploy changes.
Generation requests carry the exact upstream artifact ID in structured request
context, preventing concurrent product workflows from crossing streams.

Automatic handoffs use a bounded retry policy controlled by
`AUTOMATIC_HANDOFF_MAX_ATTEMPTS` (default `3`) and
`AUTOMATIC_HANDOFF_RETRY_DELAY_SECONDS` (default `5`). Each failure records the
attempt, HTTP status when available, capped redacted response detail, and
whether retries are exhausted. An operator can retry an approved automatic
generation request through `POST /approval-requests/{id}/retry` after correcting
the underlying issue.

Pending approvals expire after `AGENT_APPROVAL_TTL_SECONDS` (default: 24
hours). Expiration is audited as `approval_expired`, and expired requests
cannot authorize an action. The authenticated `/audit-events` endpoint returns
the latest governance events for operator review.

The executable approval actions are narrowly named and checked by the server.
`read_health_report` runs the existing read-only health-summary workflow.
`generate_product_brief` allows the Product Manager agent to produce one
structured HomeLabOps brief and engineering backlog automatically. It cannot
execute host or infrastructure actions. `approve_product_brief` publishes a
generated draft only after a separate human approval. The server verifies each action name,
approval state, and artifact association; approving any other action does not
make it executable.
`generate_technical_plan` allows the CTO agent to evaluate an approved product
brief. `approve_technical_plan` publishes the resulting technical plan only
after a separate human approval.

The repository includes an Open WebUI governance Tool at
`open-webui/functions/aicorp_approval_workflow.py`. Import it through the
administrator interface and configure its valves:

```text
agent_url=http://agent:8081
approval_token=<protected AGENT_APPROVAL_TOKEN value>
agent_approval_token=<protected per-agent approval token when delegated>
agent_name=<configured agent identity when delegated>
```

Use it to list pending requests and record an explicit human approval or denial.
It changes only the approval ledger; it cannot execute the requested action.

For repository proposals, the Software Engineer should use the dedicated
`request_repository_change_proposal_submission` tool to create the exact
`submit_repository_change_proposal` approval request, then use
`submit_software_engineer_repository_change_proposal` after that request is
approved. The generic `request_approval` tool is not needed for this workflow.

The repository also includes a separate read-only inspection tool at
`open-webui/functions/aicorp_artifact_inspector.py`. Import it separately and
configure the same valves. It exposes only the latest product brief, latest
technical plan, latest Engineering Manager plan, and worker-plan list. Enable
this smaller tool in local-model chats when reviewing artifacts; keep the
approval workflow tool for approval and governed generation actions.

## Product Manager Agent

The Product Manager agent is the first business-role agent in AICorp. It uses
the approved product context and LiteLLM to produce a structured HomeLabOps
product brief and a small engineering backlog. It stores both in the
`agent_product_briefs` PostgreSQL table with the model context, schema version,
requester, approval association, and audit events.

The lifecycle is intentionally automatic-then-human after the request is created:

1. Request a new brief through `POST /product-briefs/request`; the server
   automatically queues and dispatches `generate_product_brief`.
2. The PM generates one draft through `POST /product-briefs/generate` and the
   server creates the pending publication approval automatically.
3. Review the draft through `GET /product-briefs/latest`.
4. Approve the generated `approve_product_brief` request. Human approval
	automatically publishes that exact draft; product-brief API responses
	report its terminal status as `published`. The direct
	`POST /product-briefs/{id}/approve` route is retained as a compatibility
	endpoint.

To clear an old brief without deleting its history, the authenticated operator
can submit `POST /product-briefs/{id}/archive` with a reason. This creates a
pending `archive_product_brief` approval; it does not archive the brief by
itself. A human operator must approve that request. If the brief has a pending
publication approval, the archive submission and approval context must include
`confirm: true`; approval then cancels that publication approval atomically
with archival. Archived briefs and all plans, tasks, and proposals derived
from them are excluded from normal collection/latest views, while direct ID
lookups remain available for audit.

The PM agent does not claim customer demand has been validated. Its brief is a
decision artifact for human review, not an autonomous product commitment.

## CTO Agent

The CTO agent consumes only an approved Product Manager brief. It produces a
structured technical plan covering feasibility, architecture, data and API
contracts, security, deployment, recovery, non-functional requirements, risks,
and a refined engineering backlog. It has no shell, infrastructure, secret, or
external-communication access.

The CTO lifecycle is intentionally two-stage:

1. Approve the PM product brief.
2. Create and approve a `generate_technical_plan` request.
3. Generate one draft through `POST /technical-plans/generate` with the PM brief ID.
4. Review the draft through `GET /technical-plans/latest`.
5. Create and approve an `approve_technical_plan` request.
6. Publish the draft through `POST /technical-plans/{id}/approve`.

If a dependency is discovered after approval, request an amendment through
`POST /technical-plans/amend` with the approved parent plan ID and explicit
dependency strings. The workflow creates a new generation approval, stores the
result as a linked amendment, and creates a fresh human
`approve_technical_plan` request before any Engineering Manager handoff.

The CTO recommendation is an architecture decision artifact. It does not
authorize implementation, infrastructure changes, or production deployment.

Engineering Manager milestones are structured objects with an ID, title,
description, status, and explicit dependencies. Newly generated milestones use
`planned`; `complete` requires supporting approved execution and QA evidence.

## Engineering Manager, Software Engineer, and QA Workers

The Engineering Manager consumes only an approved CTO plan and turns it into
bounded workstreams, milestones, dependencies, definitions of done, and a test
strategy. It does not modify repositories or deploy infrastructure.

The Software Engineer and QA workers consume only an approved Engineering
Manager plan. They produce separate implementation and test handoffs; they do
not claim work is complete, modify source code, execute commands, or merge
changes.

The governed sequence is automated after the initial human product-brief approval:

1. Generate and review `POST /engineering-plans/generate`.
2. The server automatically approves the engineering-plan handoff.
3. Generate the `software_engineer` and `qa_engineer` worker plans.
4. The server automatically approves each worker-plan handoff.

These workers create planning artifacts for a future implementation workflow.
They do not yet have repository write access or execution tools.

## Governed Execution and QA

The first implementation workflow is intentionally evidence-first. It binds one
work item to an approved Software Engineer worker plan, records an
implementation submission, and then records QA validation from an approved QA
worker plan. It does not grant shell access, repository write access, commit
authority, merge authority, or deployment authority.

The lifecycle is:

1. Create and approve `start_software_engineer_execution` for one `work_item_id`.
2. Start the task through `POST /execution-tasks/start`.
3. Create and approve `submit_software_engineer_execution`.
4. Submit the changed surfaces, tests, results, and diff reference through
	`POST /execution-tasks/{id}/submit`.
5. Create and approve `record_qa_validation`.
6. Record QA pass/fail evidence through `POST /execution-tasks/{id}/qa`.

The task is `qa_passed` or `qa_failed` after validation. Intermediate approval
requests are automatically approved by the responsible-agent policy. The
server never fabricates an implementation, patch, test result, or QA result.
The initial product brief and final deployment remain the only human approval
gates.

When a proposal's implementation evidence and patch target do not match, an
operator can supersede the approved proposal without deleting its history:
`POST /change-proposals/{id}/supersede` with a required reason. A replacement
execution submission and proposal can then proceed through automated QA.

## Repository Change Proposals

After a task passes QA, the Software Engineer may submit one bounded unified
diff as a repository-change proposal. The proposal records the target files,
patch, summary, and tests, but the agent does not apply it. QA reviews the
proposal with `review_repository_change`. A passing QA review automatically
approves the proposal and creates exactly one human `deploy` approval. The
repository approval is recorded in the audit log but is not a human approval
request. The only product-to-deployment human gates are product publication
and final deployment.

## Persistence Validation

After the services become healthy:

1. Confirm all containers are running with `docker compose ps`.
2. Create only approved test data through the service interfaces.
3. Restart the stack with `docker compose restart`.
4. Confirm the services return healthy and the test data remains available.
5. Test a host reboot recovery procedure before increasing persistent data volume.

## Backup and Restore

Back up the following host paths using the approved backup process:

```text
/opt/aicorp/data/postgres
/opt/aicorp/data/redis
/opt/aicorp/data/qdrant
/opt/aicorp/data/open-webui
/opt/aicorp/config/litellm/config.yaml
```

Do not back up or publish the external environment values in this repository. A restore is not complete until the services start, health checks pass, and representative test data is verified.

## Stop and Remove Containers

Stopping containers preserves the bind-mounted data:

```bash
docker compose down
```

Do not add `--volumes` to routine shutdown commands because this stack's state must be retained. Removing persistent data requires explicit human approval and a verified backup.
