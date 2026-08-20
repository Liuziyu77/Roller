"""Roller: batch collection of agentic trajectories."""

from roller.version import __version__
from roller.bundles import BundlePolicy, TaskBundleStore
from roller.collector import CollectionConfig, Collector
from roller.models import (
    ContextFile,
    HarnessResult,
    InputFile,
    RunRequest,
    Task,
    TaskBundle,
)

__all__ = [
    "BundlePolicy",
    "CollectionConfig",
    "ContextFile",
    "Collector",
    "HarnessResult",
    "InputFile",
    "RunRequest",
    "Task",
    "TaskBundle",
    "TaskBundleStore",
]
