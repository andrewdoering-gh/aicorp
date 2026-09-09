# AICorp Architecture Decisions

## Purpose

This document records significant architectural, operational, security, and governance decisions for AICorp.

AICorp spans several repositories and infrastructure layers. These decisions provide a shared architectural baseline for `aicorp`, `drewnet-iac`, `drewnet-config`, and `drewnet-apps`.

## Decision Statuses

- **Proposed:** Under consideration; not an implementation requirement.
- **Accepted:** Approved and should guide implementation.
- **Superseded:** Replaced by a newer decision.
- **Deprecated:** Describes existing behavior but should not guide new work.
- **Rejected:** Evaluated and intentionally not selected.

Decision identifiers remain stable even if titles change. New decisions use the next available identifier.

## Decision Index

- [ADR-001: Run AICorp on a nested Proxmox node](#adr-001-run-aicorp-on-a-nested-proxmox-node)
- [ADR-002: Use separate Terraform roots and state objects](#adr-002-use-separate-terraform-roots-and-state-objects)
- [ADR-003: Use Terraform for infrastructure lifecycle](#adr-003-use-terraform-for-infrastructure-lifecycle)
- [ADR-004: Use Ansible for operating-system configuration](#adr-004-use-ansible-for-operating-system-configuration)
- [ADR-005: Use Docker Compose for the initial application platform](#adr-005-use-docker-compose-for-the-initial-application-platform)
- [ADR-006: Avoid nested ZFS and Ceph](#adr-006-avoid-nested-zfs-and-ceph)
- [ADR-007: Begin with one permanent control-plane VM](#adr-007-begin-with-one-permanent-control-plane-vm)
- [ADR-008: Use Ubuntu Server 26.04 for initial AICorp guests](#adr-008-use-ubuntu-server-2604-for-initial-aicorp-guests)
- [ADR-009: Use PostgreSQL, Redis, and Qdrant as initial state services](#adr-009-use-postgresql-redis-and-qdrant-as-initial-state-services)
- [ADR-010: Use LiteLLM as the model gateway](#adr-010-use-litellm-as-the-model-gateway)
- [ADR-011: Use Open WebUI as the initial human interface](#adr-011-use-open-webui-as-the-initial-human-interface)
- [ADR-012: Begin with hosted or external model inference](#adr-012-begin-with-hosted-or-external-model-inference)
- [ADR-013: Begin with one persistent AI agent](#adr-013-begin-with-one-persistent-ai-agent)
- [ADR-014: Require human approval for high-impact actions](#adr-014-require-human-approval-for-high-impact-actions)
- [ADR-015: Use deny-by-default agent tool authorization](#adr-015-use-deny-by-default-agent-tool-authorization)
- [ADR-016: Keep secrets outside Git](#adr-016-keep-secrets-outside-git)
- [ADR-017: Keep project responsibilities in separate repositories](#adr-017-keep-project-responsibilities-in-separate-repositories)
- [ADR-018: Use the Admin VM as the deployment execution host](#adr-018-use-the-admin-vm-as-the-deployment-execution-host)
- [ADR-019: Keep AICorp documentation in an umbrella repository](#adr-019-keep-aicorp-documentation-in-an-umbrella-repository)
- [ADR-020: Introduce private workload networking incrementally](#adr-020-introduce-private-workload-networking-incrementally)
- [ADR-021: Treat observability and auditability as platform requirements](#adr-021-treat-observability-and-auditability-as-platform-requirements)
- [ADR-022: Prefer backups over long-lived outer snapshots](#adr-022-prefer-backups-over-long-lived-outer-snapshots)
- [ADR-023: Protect persistent Terraform resources from accidental destruction](#adr-023-protect-persistent-terraform-resources-from-accidental-destruction)
- [ADR-024: Introduce complexity only after a demonstrated requirement](#adr-024-introduce-complexity-only-after-a-demonstrated-requirement)

---

## ADR-001: Run AICorp on a nested Proxmox node

**Status:** Accepted  
**Date:** 2026-09-08  
**Decision owner:** Andrew Doering

AICorp runs inside the nested Proxmox VE node `aicorp-pve01`. This provides an isolated area for AICorp virtual machines, storage, networking, and experiments while limiting the blast radius to the existing homelab control plane. The outer Proxmox environment remains a separate management boundary.

---

## ADR-002: Use separate Terraform roots and state objects

**Status:** Accepted

The physical Proxmox environment and the nested AICorp environment use separate Terraform roots and state objects. This prevents circular ownership, limits accidental blast radius, and keeps provider and recovery boundaries clear.

---

## ADR-003: Use Terraform for infrastructure lifecycle

**Status:** Accepted

Terraform is the source of truth for infrastructure resources, including Proxmox virtual machines, storage attachments, networking, and lifecycle configuration. Resources should not be routinely created or changed manually after Terraform ownership is established.

---

## ADR-004: Use Ansible for operating-system configuration

**Status:** Accepted

Ansible manages operating-system configuration and host-level platform prerequisites after virtual machines are provisioned. Packages, users, Docker installation, daemon configuration, and comparable host settings belong in the configuration repository.

---

## ADR-005: Use Docker Compose for the initial application platform

**Status:** Accepted

Docker Compose is the initial lifecycle manager for application and platform services. Compose is sufficient for the initial single-node environment and has lower operational complexity than Kubernetes.

---

## ADR-006: Avoid nested ZFS and Ceph

**Status:** Accepted

The initial AICorp environment will not deploy nested ZFS or nested Ceph. These systems add resource and failure-mode complexity without a demonstrated initial requirement.

---

## ADR-007: Begin with one permanent control-plane VM

**Status:** Accepted

AICorp begins with one permanent control-plane VM, `aicorp-control01`, rather than a cluster. One stable host is sufficient for initial validation and preserves scarce homelab resources. The initial platform is not highly available.

---

## ADR-008: Use Ubuntu Server 26.04 for initial AICorp guests

**Status:** Accepted

Initial AICorp guest VMs use Ubuntu Server 26.04 from the approved cloud template. Standardizing the guest operating system reduces configuration variance and supports repeatable automation.

---

## ADR-009: Use PostgreSQL, Redis, and Qdrant as initial state services

**Status:** Accepted

PostgreSQL, Redis, and Qdrant are the initial state services for relational data, transient or queued state, and vector retrieval. Their persistent data requires explicit volume, backup, restore, and upgrade procedures.

---

## ADR-010: Use LiteLLM as the model gateway

**Status:** Accepted

LiteLLM is the initial gateway abstraction for hosted and external model providers. It centralizes provider routing, credentials, usage accounting, and model access policy behind a standard interface.

---

## ADR-011: Use Open WebUI as the initial human interface

**Status:** Accepted

Open WebUI is the initial browser-based human interface for interacting with the model platform. It enables early validation without requiring a custom frontend before platform workflows are understood.

---

## ADR-012: Begin with hosted or external model inference

**Status:** Accepted

Initial model inference uses hosted or external providers through the model gateway. Local inference hardware is deferred because the physical homelab has limited compute capacity. External usage requires cost controls and credential protection.

---

## ADR-013: Begin with one persistent AI agent

**Status:** Accepted

The platform begins with one persistent AI agent and expands to multiple agents only after the single-agent workflow is reliable. This keeps state, authorization, observability, and failure handling understandable during initial validation.

---

## ADR-014: Require human approval for high-impact actions

**Status:** Accepted

Human approval is required before infrastructure destruction, external communications, public publication, paid API usage above established limits, secret access, privilege elevation, new tool authorization, production deployment, authorization-policy changes, or backup and retention-policy changes.

---

## ADR-015: Use deny-by-default agent tool authorization

**Status:** Accepted

Agents receive no tool access by default. Each permitted tool must be explicitly authorized for the agent's role and context. Tool permissions must be reviewable, attributable, and change-controlled.

---

## ADR-016: Keep secrets outside Git

**Status:** Accepted

Secrets, credentials, private keys, tokens, passwords, and environment-specific secret values remain outside Git. Source-controlled files may reference externally injected values or safe examples only. Secret injection and rotation are documented in the runbook.

---

## ADR-017: Keep project responsibilities in separate repositories

**Status:** Accepted

Infrastructure lifecycle belongs in `drewnet-iac`, operating-system and platform configuration belongs in `drewnet-config`, application and product code belongs in `drewnet-apps`, and project documentation belongs in `aicorp`.

---

## ADR-018: Use the Admin VM as the deployment execution host

**Status:** Accepted

Routine deployment, configuration, validation, and recovery commands execute from the designated Admin VM using the checked-out repositories and protected external credentials. Centralized execution provides a consistent network location, toolchain, and operator boundary.

---

## ADR-019: Keep AICorp documentation in an umbrella repository

**Status:** Accepted

Project-wide architecture, decisions, operating procedures, and coordination documentation remain in the `aicorp` umbrella repository. Implementation repositories may contain local documentation, but project-wide decisions have one authoritative location.

---

## ADR-020: Introduce private workload networking incrementally

**Status:** Accepted

Private workload networking is introduced as a demonstrated isolation requirement emerges. The initial deployment uses the simplest network arrangement that meets its security and operational needs, avoiding premature routing and troubleshooting complexity.

---

## ADR-021: Treat observability and auditability as platform requirements

**Status:** Accepted

Agent decisions, tool calls, approvals, failures, significant state transitions, and relevant platform events must be logged in an attributable and reviewable way. Observability and audit records are part of platform design.

---

## ADR-022: Prefer backups over long-lived outer snapshots

**Status:** Accepted

Tested backups are preferred for recovery. Long-lived snapshots on the outer Proxmox environment are not the primary backup strategy. Backup and restore procedures must be documented and tested before persistent workload volume increases.

---

## ADR-023: Protect persistent Terraform resources from accidental destruction

**Status:** Accepted

Persistent infrastructure resources, including the nested Proxmox node and permanent control-plane VM, use Terraform lifecycle protection against accidental destruction where supported. Intentional destruction requires an explicit reviewed change and a confirmed recovery plan.

---

## ADR-024: Introduce complexity only after a demonstrated requirement

**Status:** Accepted

New platforms, services, abstractions, agents, network layers, and operational processes are introduced only when a demonstrated requirement justifies their cost and complexity. The project therefore favors one-agent workflows, Docker Compose, hosted model routing, and straightforward storage and networking until evidence supports expansion.
