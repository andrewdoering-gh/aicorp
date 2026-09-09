# AICorp

AICorp is an AI-native virtual company running in the Drewnet homelab. The project combines virtual infrastructure, configuration management, containerized platform services, retrieval-augmented generation, and autonomous AI agents into a reproducible company operating environment.

AICorp is designed as a working engineering system, not solely as a conversational simulation. AI agents will eventually plan products, create software, review work, maintain company knowledge, and report results within explicit authority and safety boundaries.

Andrew serves as the owner, board, and human operator of AICorp.

## Project Goals

AICorp provides a practical environment for developing skills and reusable patterns in:

- AI agent orchestration
- Retrieval-augmented generation
- Agent memory and state management
- Model gateways and inference platforms
- AI observability and governance
- Infrastructure as Code
- Configuration management
- Containerized application platforms
- Automated software development workflows
- Human oversight of autonomous systems

The long-term objective is to operate a small virtual company in which AI agents can collaborate on useful products while remaining observable, auditable, resource-conscious, and subject to human approval.

## Design Principles

1. Infrastructure must be reproducible.
2. Terraform manages infrastructure lifecycle.
3. Ansible manages operating-system configuration.
4. Docker Compose initially manages application services.
5. Secrets must never be committed to Git.
6. Outer and nested Proxmox environments use separate Terraform states.
7. AI agents receive only the tools required for their assigned roles.
8. Destructive, external, or costly actions require human approval.
9. Agent activity must be logged and auditable.
10. Components should be replaceable without rebuilding the entire environment.
11. The architecture must respect the resource limits of the physical homelab.
12. Complexity must be introduced only when it solves a demonstrated requirement.

## Repository Architecture

AICorp spans several Drewnet repositories.

```text
AICorp
├── drewnet-iac
│   └── Infrastructure lifecycle and virtual resources
├── drewnet-config
│   └── Operating-system and platform configuration
├── drewnet-apps
│   └── Application services, agents, and product code
└── aicorp
    └── Project-level documentation and coordination