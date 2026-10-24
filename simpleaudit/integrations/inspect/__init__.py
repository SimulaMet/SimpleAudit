"""Inspect AI integration.

Run SimpleAudit scenario packs as an Inspect task, with Inspect's model roles
for the auditor and the judge::

    inspect eval simpleaudit/audit -T pack=nav_aap \\
        --model anthropic/claude-haiku-5-5 --model-role grader=anthropic/claude-sonnet-5-5

and read Inspect logs back as SimpleAudit results, so ``rejudge``,
``reframing_check`` and the stability report work on them::

    from simpleaudit.integrations.inspect import load_inspect_log

    runs = load_inspect_log("logs/2026-10-24T10-00-00_audit.eval")

Needs ``pip install 'simpleaudit[inspect]'``.
"""

try:
    import inspect_ai  # noqa: F401
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError(
        "simpleaudit.integrations.inspect needs Inspect AI: pip install 'simpleaudit[inspect]'"
    ) from exc

from ._client import InspectModelClient
from ._log import load_inspect_log
from ._task import (
    STORE_KEY,
    audit,
    safety_score,
    severity_counts,
    simpleaudit_scorer,
    simpleaudit_solver,
)

__all__ = [
    "InspectModelClient",
    "STORE_KEY",
    "audit",
    "load_inspect_log",
    "safety_score",
    "severity_counts",
    "simpleaudit_scorer",
    "simpleaudit_solver",
]
