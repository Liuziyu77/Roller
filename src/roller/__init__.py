"""Roller: batch collection of agentic trajectories."""

from roller.version import __version__
from roller.bundles import BundlePolicy, TaskBundleStore
from roller.collector import CollectionConfig, Collector
from roller.conversation import ConversationManager
from roller.models import (
    ContextFile,
    HarnessResult,
    InputFile,
    RunRequest,
    Task,
    TaskBundle,
    UserSimulatorSpec,
)

__all__ = [
    "BundlePolicy",
    "CollectionConfig",
    "ContextFile",
    "Collector",
    "ConversationManager",
    "HarnessResult",
    "InputFile",
    "RunRequest",
    "Task",
    "TaskBundle",
    "TaskBundleStore",
    "UserSimulatorSpec",
]
