"""The escalation client: a small Jira Cloud REST client (spec §12)."""

from ultron.jira.body import AttemptRow, TicketBody, ticket_summary, to_adf
from ultron.jira.client import (
    ENDPOINTS,
    ENV_VARS,
    EscalationResult,
    IssueType,
    JiraClient,
    JiraConfigError,
    JiraCredentials,
    JiraError,
    JiraTarget,
    JiraUnreachable,
    SprintChoice,
)

__all__ = [
    "ENDPOINTS",
    "ENV_VARS",
    "AttemptRow",
    "EscalationResult",
    "IssueType",
    "JiraClient",
    "JiraConfigError",
    "JiraCredentials",
    "JiraError",
    "JiraTarget",
    "JiraUnreachable",
    "SprintChoice",
    "TicketBody",
    "ticket_summary",
    "to_adf",
]
