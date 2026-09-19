WORKSTREAM_FILE_GROUPS = {
    "WS-01": (
        "aicorp/agent/agent.py",
        "aicorp/agent/execution.py",
    ),
    "WS-02": (
        "aicorp/agent/workers.py",
        "aicorp/agent/notifications.py",
    ),
    "WS-03": (
        "aicorp/compose.yaml",
        "aicorp/config/prometheus/prometheus.yml",
        "aicorp/knowledge/docs/RUNBOOK.md",
    ),
}
WORKSTREAM_IDS = frozenset(WORKSTREAM_FILE_GROUPS)
WORKSTREAM_ORDER = ("WS-01", "WS-02", "WS-03")
WORKSTREAM_PREDECESSORS = {
    workstream_id: WORKSTREAM_ORDER[:index]
    for index, workstream_id in enumerate(WORKSTREAM_ORDER)
}