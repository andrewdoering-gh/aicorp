# AICorp Project

## Document Purpose

This document is the authoritative project definition for AICorp.

AICorp spans multiple Drewnet repositories and infrastructure layers. This document records:

- Project goals
- Scope and boundaries
- Current architecture
- Repository responsibilities
- Infrastructure ownership
- Technical standards
- Security and governance requirements
- Resource constraints
- Implementation milestones
- Current project status
- Instructions for human and AI contributors

The root `README.md` provides a concise introduction to AICorp. This document provides the detailed context needed to design, implement, operate, and extend the platform.

## Project Summary

AICorp is an AI-native virtual company running in the Drewnet homelab.

The environment will combine:

- Virtual infrastructure
- Infrastructure as Code
- Configuration management
- Containerized platform services
- Model gateways
- Retrieval-augmented generation
- Persistent AI agents
- Agent tools
- Company knowledge
- Human approval workflows
- Application and product development

AICorp is intended to become a functioning engineering environment rather than only a simulation of conversations among AI personas.

AI agents will eventually be able to:

- Interpret company objectives
- Propose products and projects
- Produce technical plans
- Create and review software
- Perform controlled testing
- Maintain company knowledge
- Report progress and performance
- Collaborate with other agents
- Escalate decisions to a human operator

All agent capabilities must operate within explicit authorization, resource, cost, security, and human-approval boundaries.

Andrew serves as:

- Project owner
- Board of directors
- Infrastructure operator
- Security authority
- Budget authority
- Final approval authority

## Project Objectives

### Primary Objective

Build a reproducible AI-native company platform in the Drewnet homelab where persistent AI agents can collaborate on useful work under human supervision.

### Technical Objectives

The project should provide hands-on experience with:

- AI agent orchestration
- Persistent agent execution
- Agent memory architecture
- Retrieval-augmented generation
- Vector databases
- Structured agent state
- Model gateways
- Local and hosted model integration
- AI usage accounting
- Agent authorization
- Human approval workflows
- AI observability
- AI governance
- Infrastructure as Code
- Configuration management
- Container operations
- Automated software development
- Platform backup and recovery
- Secure secret handling

### Product Objective

AICorp should produce at least one useful software product.

Initial candidates include:

- BackupGPT
- HomeLabOps

The first product should align with:

- Available homelab capacity
- Existing systems engineering knowledge
- Existing data protection expertise
- Opportunities to learn AI engineering
- The ability to validate results objectively

### Career Development Objective

AICorp should provide demonstrable experience designing and operating:

- AI platforms
- Agent-based systems
- RAG systems
- Model abstractions
- Autonomous workflows
- AI infrastructure
- AI governance controls

Project artifacts should be suitable for future portfolio demonstrations after secrets, private addresses, and sensitive operational details are removed.

## Success Criteria

AICorp will be considered successful when the platform can demonstrate all of the following:

1. Infrastructure can be recreated from version-controlled definitions.
2. Operating-system configuration is automated and idempotent.
3. Core state services survive VM and container restarts.
4. At least one model can be accessed through a standard gateway.
5. A human operator can interact with the platform through a web interface.
6. A persistent agent can store and retrieve company knowledge.
7. A persistent agent can execute a scheduled workflow.
8. Agent actions are logged and attributable.
9. Destructive or costly actions require human approval.
10. Secrets remain outside source control.
11. Backup and restore procedures are tested.
12. At least one useful product or working prototype is produced.

## Non-Goals

The initial platform is not intended to provide:

- Production-grade public hosting
- High availability
- A multi-node Proxmox cluster
- Nested Ceph
- Nested ZFS
- Kubernetes
- Unrestricted autonomous infrastructure changes
- Unrestricted internet browsing by agents
- Unrestricted external communications
- Autonomous financial transactions
- Autonomous production deployments
- Enterprise-scale local model inference
- A large number of agents before one-agent workflows are reliable
- Public multi-tenant access

These capabilities may be evaluated later if a demonstrated requirement justifies the added complexity.

## Design Principles

### Reproducibility

Infrastructure and configuration must be reproducible from version-controlled definitions.

Manual actions should be limited to:

- Initial bootstrapping
- Credential creation
- Secret injection
- Destructive storage maintenance
- Recovery activities
- Actions that cannot be safely automated

Where a manual action is necessary, the action must be documented in the project runbook.

### Clear Ownership

Each layer has a defined management tool:

- Terraform manages infrastructure resources.
- Ansible manages operating-system configuration.
- Docker Compose manages application services.
- Application code manages business and agent behavior.
- Documentation records architecture, operations, and decisions.

Resources should not be managed manually after ownership has been assigned to an automation tool.

### State Separation

The physical Proxmox environment and nested AICorp environment must use separate Terraform state objects.

This prevents:

- Circular resource ownership
- Accidental self-management
- Broad blast radius
- Confusing provider configuration
- Destructive changes across environment boundaries

### Least Privilege

Users, services, tokens, and agents should receive only the permissions required for their assigned responsibilities.

### Human Oversight

Human approval is required for actions involving:

- Infrastructure destruction
- External communications
- Public content publication
- Paid API usage above established limits
- Secret access
- Privilege elevation
- New tool authorization
- Production deployment
- Changes to authorization policy
- Changes to backup or retention policy

### Resource Awareness

AICorp shares a modest physical server with existing Drewnet services. Resource allocations must be intentional and monitored.

### Incremental Complexity

New components should be introduced only after simpler components are validated.

Examples:

- Validate one persistent agent before building a multi-agent hierarchy.
- Validate Docker Compose before considering Kubernetes.
- Validate hosted model routing before adding local inference hardware.
- Validate backup procedures before increasing persistent data volume.
- Validate manual approval workflows before allowing autonomous actions.

### Auditability

Agent decisions, tool calls, approvals, failures, and significant state transitions should be recorded.

## Repository Architecture

AICorp spans four repositories.

AICorp
├── aicorp
│   └── Umbrella documentation and project coordination
├── drewnet-iac
│   └── Infrastructure lifecycle
├── drewnet-config
│   └── Operating-system and platform configuration
└── drewnet-apps
    └── Applications, agents, and products