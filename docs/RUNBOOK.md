# AICorp Operations Runbook

## Purpose

This runbook documents the procedures used to deploy, validate, operate, troubleshoot, back up, and recover the AICorp environment.

AICorp spans four repositories:

```text
aicorp          Project documentation and architecture
drewnet-iac     Terraform infrastructure definitions
drewnet-config  Ansible operating-system and platform configuration
drewnet-apps    Application services, agents, and product code
```

## Operational Principles

1. Review changes before applying them.
2. Never bypass Terraform state locking for routine operations.
3. Never apply a Terraform plan containing unexpected destruction.
4. Never store credentials in Git.
5. Use Terraform for infrastructure lifecycle.
6. Use Ansible for operating-system configuration.
7. Use Docker Compose for application lifecycle.
8. Prefer backups over long-lived outer snapshots.
9. Validate one layer at a time.
10. Record significant changes and failures.
11. Keep recovery procedures tested.
12. Preserve capacity for existing Drewnet workloads.
13. Require human approval for destructive or externally impactful actions.

## Environment Summary

### Physical Proxmox Host

```text
Node:                   pve
Management address:     192.168.3.10
Management network:     192.168.3.0/24
Gateway:                192.168.3.1
Management bridge:      vmbr0
Primary VM storage:     local-lvm
Backup and ISO storage: backup-nvme
CPU:                    Intel Core i5-8600K
Physical cores:         6
Memory:                 Approximately 64 GB
```

### Nested Proxmox Node

```text
Name:                   aicorp-pve01
Outer VMID:             1300
Management address:     192.168.3.100
Virtual CPUs:           4
Memory:                 32 GB
System disk:            128 GB
CPU type:               host
```

### AICorp Guests

```text
Ubuntu cloud template:  ubuntu-26.04-cloud-template
Template inner VMID:    9008

Control-plane name:     aicorp-control01
Control-plane inner VMID: 1200
Operating system:       Ubuntu Server 26.04
Virtual CPUs:           2
Memory:                 8 GB
Current address:        192.168.3.101
DNS name:               control01.home.arpa
MAC address:            BC:24:11:3C:F9:93
```

The control-plane host uses `control01.home.arpa`, which must resolve to `192.168.3.101`. Update this section when infrastructure assignments change.

## Repository Locations

The repositories are checked out under:

```text
~/git/drewnet/aicorp/
~/git/drewnet/drewnet-iac/
~/git/drewnet/drewnet-config/
~/git/drewnet/drewnet-apps/
```

The designated Admin VM is the deployment execution host. Credentials are loaded from protected external locations and must never be printed or committed.

## Git Safety Checks

Before changing a repository:

```bash
git status
git diff --check
git diff
```

Look for generated or sensitive files without printing their contents:

```bash
git status --short
```

Do not commit environment files, Terraform state, saved plans, variable files containing environment values, private keys, tokens, passwords, or other credentials.

## Outer Terraform Operations

The outer Terraform root manages resources on the physical Proxmox node, including the nested Proxmox VM.

Location:

```text
~/git/drewnet/drewnet-iac/terraform/proxmox
```

The outer and inner Terraform roots use separate state objects. Load the required protected credentials using the local operator procedure without displaying their values.

Validate and plan:

```bash
cd ~/git/drewnet/drewnet-iac/terraform/proxmox
terraform init
terraform fmt -recursive
terraform validate
terraform plan -lock-timeout=60s
```

Apply only an expected, reviewed plan:

```bash
terraform apply -lock-timeout=60s
```

Never use `-lock=false` for routine operations. Do not apply a plan containing unexpected destruction or replacement.

## Inner Terraform Operations

The inner Terraform root manages resources inside `aicorp-pve01`.

Location:

```text
~/git/drewnet/drewnet-iac/terraform/aicorp
```

Use the approved local wrapper or equivalent operator procedure so the correct protected credentials are loaded without exposing them.

Validate and plan:

```bash
cd ~/git/drewnet/drewnet-iac/terraform/aicorp
terraform init
terraform fmt -recursive
terraform validate
terraform plan -lock-timeout=60s
```

Apply only an expected, reviewed plan:

```bash
terraform apply -lock-timeout=60s
```

The expected initial relationships are:

```text
Ubuntu template VMID: 9008
Control-plane VMID:   1200
Control-plane clone:  Ubuntu template
```

Do not modify Terraform resources or state as part of routine Ansible configuration work.

## Ansible Configuration

Ansible manages the operating-system and host platform configuration. The AICorp Ansible root is:

```text
~/git/drewnet/drewnet-config/ansible/aicorp
```

The AICorp inventory targets only `aicorp-control01`:

```text
control01.home.arpa -> 192.168.3.101
```

The AICorp configuration reuses the shared homelab `ubuntu_baseline` role through a path-based role lookup. It also uses the local AICorp `docker` role. Node Exporter is deferred until the monitoring and scrape design is established.

### Install Ansible Collection Dependencies

```bash
cd ~/git/drewnet/drewnet-config/ansible/aicorp
ansible-galaxy collection install -r requirements.yml
```

### Verify Connectivity

```bash
ansible \
  -i inventory/hosts.yml \
  aicorp-control01 \
  -m ansible.builtin.ping
```

Expected result:

```text
aicorp-control01 | SUCCESS => ...
```

### Syntax Check

```bash
ansible-playbook \
  -i inventory/hosts.yml \
  playbooks/control-plane.yml \
  --syntax-check
```

### Check Mode

Review the proposed changes without applying them:

```bash
ansible-playbook \
  -i inventory/hosts.yml \
  playbooks/control-plane.yml \
  --check \
  --diff \
  --limit aicorp-control01
```

### Deploy the Control-Plane Configuration

Apply only after reviewing check-mode output:

```bash
ansible-playbook \
  -i inventory/hosts.yml \
  playbooks/control-plane.yml \
  --limit aicorp-control01
```

The playbook configures:

- Hostname `aicorp-control01` and its local hosts entry.
- The shared Ubuntu baseline.
- Package upgrades and baseline administration packages.
- Chrony time synchronization.
- QEMU guest-agent enablement.
- Docker CE from Docker's official Ubuntu repository.
- Docker Compose v2.
- `drew` membership in the `docker` group.
- Bounded Docker `json-file` container logs.
- Persistent directories under `/opt/aicorp`.

### Verify Idempotency

Run the same playbook again:

```bash
ansible-playbook \
  -i inventory/hosts.yml \
  playbooks/control-plane.yml \
  --limit aicorp-control01
```

The second run should report no unexpected changes. A new login session may be required before the Docker group membership is visible to `drew`.

### Validate the Configured Host

```bash
ansible \
  -i inventory/hosts.yml \
  aicorp-control01 \
  -b \
  -m ansible.builtin.command \
  -a 'hostnamectl --static'
```

```bash
ansible \
  -i inventory/hosts.yml \
  aicorp-control01 \
  -b \
  -m ansible.builtin.command \
  -a 'docker compose version'
```

```bash
ansible \
  -i inventory/hosts.yml \
  aicorp-control01 \
  -b \
  -m ansible.builtin.systemd_service \
  -a 'name=qemu-guest-agent enabled=true state=started'
```

Check the Docker group and persistent directories through a reviewed, non-secret command when required. Do not print `/etc/docker/daemon.json` if local policy treats host configuration as sensitive.

## Monitoring and Alert Delivery

Prometheus and Alertmanager run on `aicorp-control01`. Prometheus evaluates the
version-controlled rules and sends grouped firing and resolved alerts to
Alertmanager. Alertmanager forwards those alerts to the receiver configured by
the protected `ALERTMANAGER_WEBHOOK_URL` value in `/opt/aicorp/.env`.

The webhook receiver owner is responsible for acknowledging delivery failures,
maintaining the endpoint, and defining the human escalation path. The AICorp
operator remains responsible for infrastructure, host, storage, and service
alerts until a separate on-call owner is assigned.

### Validate Monitoring Services

```bash
docker compose ps prometheus alertmanager alertmanager-config
docker ps --filter name=aicorp-prometheus --filter name=aicorp-alertmanager
```

Validate the deployed Alertmanager configuration without printing it:

```bash
docker exec aicorp-alertmanager \
  amtool check-config /etc/alertmanager/alertmanager.yml
```

Check Prometheus rule loading and Alertmanager connectivity from the local
control-plane interfaces:

```bash
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:9093/-/ready
curl -fsS http://127.0.0.1:9090/api/v1/rules
curl -fsS http://127.0.0.1:9090/api/v1/alertmanagers
```

The Prometheus UI is available through an SSH tunnel on local port `9090` and
the Alertmanager UI through local port `9093`. Neither service is exposed
directly on the workload network.

### Test Notification Delivery

Sending a synthetic alert is an external communication and requires human
approval under ADR-014. Perform this test only after confirming that the
configured receiver is a test destination or that the notification is expected.

With approval, submit a short-lived synthetic alert to Alertmanager:

```bash
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  --data '[
    {
      "labels": {
        "alertname": "AICorpNotificationTest",
        "severity": "info",
        "host": "aicorp-control01"
      },
      "annotations": {
        "summary": "AICorp notification delivery test",
        "description": "Synthetic test alert; no infrastructure action is required."
      },
      "startsAt": "2026-01-01T00:00:00Z",
      "endsAt": "2026-01-01T00:05:00Z",
      "generatorURL": "http://localhost:9090/graph"
    }
  ]' \
  http://127.0.0.1:9093/api/v1/alerts
```

Verify the receiver records one firing notification and one resolved
notification. Do not treat a successful HTTP response from Alertmanager as
proof that the external receiver accepted the message; verify at the receiver.

### Delivery and Escalation Expectations

- Alertmanager groups alerts by `alertname` and `host`.
- Initial grouping waits 30 seconds.
- Subsequent grouped updates use a 5-minute interval.
- Repeated unresolved alerts are resent every 4 hours.
- Resolved alerts are sent because `send_resolved` is enabled.
- The external receiver owns endpoint availability and downstream escalation.
- A failed notification route must be recorded and manually escalated until a
  replacement route is approved.

Do not place webhook URLs, receiver credentials, notification payloads that
contain secrets, or provider tokens in Git or in incident notes.

## Application Operations

Applications are managed from `drewnet-apps` with Docker Compose. Do not add or start application containers as part of the control-plane configuration milestone.

### HomeLabOps Product Prototype

The AICorp application repository includes the read-only HomeLabOps prototype.
It presents monitored service health, active Prometheus alerts, and the latest
completed agent brief in one operator view. It does not execute commands,
change infrastructure, restart services, access secrets, or bypass approvals.

After deploying the platform, create a local SSH tunnel:

```bash
ssh -L 8081:127.0.0.1:8081 drew@control01.home.arpa
```

Open `http://127.0.0.1:8081/product` in the local browser. The page reads its
status from the agent's read-only `/product-data` endpoint. Treat this as a
prototype product workflow, not a public service; do not expose port 8081
outside the reviewed operator access path.

The Product Manager workflow is separate from this operational view. Create an
approved `generate_product_brief` request, use the authenticated PM workflow to
generate and review one draft, then create a separate approved
`approve_product_brief` request before publishing it. The PM agent produces
product decisions and engineering backlog proposals; it has no shell,
infrastructure, secret, or external-communication access.

The CTO agent follows the approved PM artifact. It generates a technical plan
only when the referenced product brief is already approved and the operator has
approved `generate_technical_plan`. Review the draft through
`GET /technical-plans/latest`, then use a separate `approve_technical_plan`
approval before publishing it. The CTO plan recommends architecture and work;
it does not authorize implementation or infrastructure changes.

The next governed handoff is the Engineering Manager plan. It requires an
approved CTO plan and a `generate_engineering_plan` approval. After review and
an `approve_engineering_plan` approval, generate separate worker plans for
`software_engineer` and `qa_engineer`. Use the role-specific approval actions
`generate_software_engineer_plan`, `approve_software_engineer_plan`,
`generate_qa_plan`, and `approve_qa_plan`.

All three workers are planning-only. They do not write repositories, run shell
commands, deploy infrastructure, merge code, or mark work complete. Their
outputs are handoff artifacts for the next explicitly approved implementation
milestone.

Before an application deployment:

1. Validate the host configuration.
2. Review the Compose file and environment injection method.
3. Confirm required persistent directories and backups.
4. Obtain human approval for externally impactful or costly actions.
5. Apply the smallest reviewed Compose change.

## Backup and Recovery

Prefer tested backups over long-lived outer snapshots. Before increasing persistent workload data, verify that backup and restore procedures are documented and tested.

### Durable AICorp Backup Destination

Backups must be written to a mounted durable destination, not the control-plane
root disk. The standard destination is:

```text
/mnt/backup/aicorp
```

Install the reviewed backup procedure from the Admin VM:

```bash
cd ~/git/drewnet/drewnet-config/ansible/aicorp
ansible-playbook \
  -i inventory/hosts.yml \
  playbooks/backup.yml \
  --limit aicorp-control01
```

Before running a backup, mount the approved backup storage at `/mnt/backup`.
The script refuses to run when that mount is absent or has less than 5 GiB
available.

Run manually on `control01`:

```bash
sudo /usr/local/sbin/aicorp-backup
```

The procedure creates:

```text
/mnt/backup/aicorp/<timestamp>/aicorp-postgres.dump
/mnt/backup/aicorp/<timestamp>/aicorp-files.tar.gz
```

Each artifact has a SHA-256 checksum. The backup includes PostgreSQL logical
state, Redis, Qdrant, Open WebUI, Prometheus, Alertmanager, LiteLLM
configuration, and the approved knowledge directory. It deliberately excludes
`/opt/aicorp/.env`; credentials remain under the protected credential process.

The script retains the seven newest backup sets. A backup is not considered
validated until its checksums pass and the archive has been restored into a
separate test directory or database.

Recovery activities may include:

- Restoring application data from a verified backup.
- Recreating a guest from version-controlled Terraform definitions.
- Reapplying Ansible configuration from the Admin VM.
- Re-injecting credentials through protected external locations.

Do not place restored credentials, state files, saved plans, or private keys in Git.

## Terraform State Lock Recovery

Never disable state locking for routine operations. If a lock error occurs:

1. Confirm that no Terraform process is actively running.
2. Allow an active operation to finish or terminate it cleanly.
3. Use the lock ID from the error only after confirming the lock is stale.
4. Run the approved `force-unlock` procedure for the affected Terraform root.
5. Re-run a locked plan and review it before applying.

## Rollback

For Ansible configuration changes:

```bash
cd ~/git/drewnet/drewnet-config

git status
git diff -- ansible/aicorp
```

Restore the last approved configuration revision through a reviewed Git operation, then rerun the previous known-good playbook against only `aicorp-control01`.

Removing a role from a playbook does not automatically uninstall packages or delete persistent directories. Do not remove Docker, user memberships, or data without an explicit recovery decision.

For Terraform changes, do not roll back state or resources as an Ansible recovery step. Review the Terraform plan and use the appropriate infrastructure recovery procedure instead.

## Human Approval Requirements

Human approval is required for:

- Infrastructure destruction.
- External communications.
- Public content publication.
- Paid API usage above established limits.
- Secret access.
- Privilege elevation.
- New tool authorization.
- Production deployment.
- Authorization-policy changes.
- Backup or retention-policy changes.
