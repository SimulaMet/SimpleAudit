"""
Target abstractions for SimpleAudit.

A ``Target`` is anything the auditor sends a message to and receives a
response from. The core execution path depends only on the ``Target``
protocol, not on any specific model SDK or transport:

    response = await target.send(messages=..., context=...)

Concrete targets:
    - ``ModelTarget``    — an LLM endpoint via AnyLLM (the historical default)
    - ``HTTPAppTarget``  — an external application over HTTP (black-box)
    - ``CallableTarget`` — an in-process Python callable (handy for tests)

The model integration (AnyLLM) is an implementation detail of
``ModelTarget``; it is not the architectural center of the engine.
"""

from .base import Target, TargetContext, TargetResponse
from .callable import CallableTarget
from .http import HTTPAppTarget
from .model import ModelTarget

__all__ = [
    "Target",
    "TargetContext",
    "TargetResponse",
    "ModelTarget",
    "HTTPAppTarget",
    "CallableTarget",
]
