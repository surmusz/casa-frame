"""
CASA 多 Agent 协同架构 — Contract + Artifact + Scope + Authority

快速开始: from casa import init_config, Orchestrator, ArtifactStore, Contract
完整符号: from casa import <name> 或 from casa.<module> import <name>
"""

from __future__ import annotations

from .config import init_config, get_config, CASAConfig
from .artifact import ArtifactStore, ArtifactDAG
from .contract import Contract, ContractGate
from .authority import CapabilityMatrix, CapabilityRow
from .orchestration import (
    CompileRequest,
    Orchestrator,
    PlanCompiler,
    PlanExecutor,
    SimpleAgentExecutor,
    StageRunner,
)
from ._version import __version__
from . import _exports

__all__ = _exports.__all__ + ["__version__"]


def __getattr__(name: str):
    if name == "__version__":
        raise AttributeError(name)
    return _exports.resolve(name)


def __dir__() -> list[str]:
    return sorted(set(__all__))
