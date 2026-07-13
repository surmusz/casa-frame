# CASA 多 Agent 协同架构 — 可复用模块

> **一句话哲学**：让 LLM 做判断，让系统保证秩序。Agent 之间不传「对话」，只传「产物」；不靠「信任」，只靠「授权」。

**CASA** = **C**ontract（契约）+ **A**rtifact（产物）+ **S**cope（域）+ **A**uthority（授权）

**当前版本**：1.1.0

**作者**：surmusz@gmail.com 欢迎联系、讨论、PR、Issue

**仓库**：[github.com/surmusz/casa-frame](https://github.com/surmusz/casa-frame)  
**理论稿**：[Gist](https://gist.github.com/surmusz/c12bc714a4aae25b676d3f7d113dcd34)

## TL;DR (English)

**CASA** = **C**ontract + **A**rtifact + **S**cope + **A**uthority.

Multi-agent systems often share one growing chat as both control plane and data bus.
That works for demos and breaks for long, governed pipelines.

CASA keeps order in the system, not in the prompt:

- **Contract** — structured intent (not verbal overrides)
- **Artifact** — typed outputs as the only agent-to-agent I/O
- **Scope** — who can see what (`job` / `session` / …)
- **Authority** — tool permission ∩ data permission

Vs chatty **Team/Crew** and linear **n8n-style** context stuffing: fewer tokens, lower latency, auditable traces — see the [theory Gist](https://gist.github.com/surmusz/c12bc714a4aae25b676d3f7d113dcd34) (charts included).

```bash
pip install -e ".[dev]"
python examples/minimal/run.py
```

License: **AGPL-3.0-or-later**. Commercial use that cannot comply: email `surmusz@gmail.com`.

---

## 仓库结构

本目录即 **git 仓库根**（仓库名 / PyPI 包名 `casa-frame`）：

```
casa-frame/              # ← 你在这里
├── README.md              # 本文件 — 使用指南
├── LICENSE                # AGPL-3.0（社区）
├── pyproject.toml         # 包配置与可选依赖
├── casa/                  # Python 包（仅源码 + SQL schema）
│   ├── schemas/           # PG 参考 schema
│   └── *.py               # 模块源码
├── tests/                 # 单元 / 端到端测试
├── scripts/casa_smoke.py  # 手动烟雾测试
├── examples/              # 场景示例
│   ├── minimal/           # 最小 4-Agent 流水线
│   ├── rag_qa/            # RAG 问答
│   ├── multi_perspective/ # 多视角分析
│   ├── kb_rag/            # 知识库 RAG
│   ├── code_gen/          # 代码生成 + Git Hook
│   └── creative_verifier.py
└── docs/                  # 设计文档与扩展指南
    ├── README.md          # 文档索引
    ├── EXTENDING.md       # ABC 扩展点
    └── CASA-MultiAgent-Methodology.md
```

---

## 目录

0. [TL;DR (English)](#tldr-english)
1. [快速开始（5 分钟）](#1-快速开始5-分钟)
2. [模块结构](#2-模块结构)
3. [四支柱详解](#3-四支柱详解)
   - [Contract 契约](#31-contract-契约)
   - [Artifact 产物](#32-artifact-产物)
   - [Scope 域](#33-scope-域)
   - [Authority 授权](#34-authority-授权)
4. [编排层](#4-编排层)
5. [对接指南](#5-对接指南)
6. [扩展示例](#6-扩展示例)
7. [配置参考](#7-配置参考)
8. [可观测性](#8-可观测性)
9. [扩展生态](#9-扩展生态)
10. [架构总图](#10-架构总图)

---

## 1. 快速开始（5 分钟）

### 1.1 安装

```bash
# 推荐：pip 安装（开发模式）
pip install -e ".[dev]"

# 或使用 casa init 脚手架
casa init ./my_project

# 传统方式：复制 casa/ 目录到项目
cp -r casa/ your_project/
```

### 1.2 最小体验（4 Agent 经典流水线）

```python
from casa import (
    init_config, PlanCompiler, PlanExecutor, StageRunner,
    SimpleAgentExecutor, Orchestrator, ArtifactStore,
)
from casa.orchestration import CompileRequest

init_config(artifact_base_dir="my_jobs")

agent_io = {
    "data_fetcher":   ([],              "raw_data"),
    "theme_analyst":  (["raw_data"],    "theme_analytics"),
    "report_writer":  (["theme_analytics"], "report_content"),
    "qa_checker":     (["report_content"],  "qa_report"),
}

import asyncio

async def main():
    compiler = PlanCompiler(agent_io_map=agent_io, core_pipeline_ids={"data_fetcher"})
    store = ArtifactStore(job_id="demo_001")
    store.init_plan("plan_001")
    runner = StageRunner(store=store, executor=SimpleAgentExecutor({
        "data_fetcher": lambda c: {"items": [1, 2, 3]},
        "theme_analyst": lambda c: {"themes": ["A", "B"]},
        "report_writer": lambda c: {"title": "Report"},
        "qa_checker": lambda c: {"score": 0.95},
    }))
    orch = Orchestrator(compiler=compiler, executor=PlanExecutor(store=store, stage_runner=runner))
    result = await orch.run(CompileRequest(
        seed_stages=[{"agent_id": a} for a in agent_io],
    ))
    print([k for k in store.list_artifacts()])

asyncio.run(main())
```

### 1.3 完整体验（Agent Loop）

```python
import asyncio
from casa import (
    init_config, LLMProviderConfig,
    CapabilityMatrix, CapabilityRow,
    IntentRouter,
    PlanCompiler, PlanExecutor, StageRunner, MockAgentExecutor,
    Orchestrator, InterruptController,
)
from casa.orchestration import CompileRequest, UsagePolicy
from casa.intent import AgentCapability

# ─── 1. 配置（支持多 provider 凭据）───
init_config(
    artifact_base_dir="my_jobs",
    llm_default_provider="openai",
    llm_api_key="sk-openai-key",
    llm_providers={
        "anthropic": LLMProviderConfig(
            provider="anthropic",
            api_key="sk-anthropic-key",
            base_url="https://api.anthropic.com",
        ),
    },
)

# ─── 2. 领域 Agent I/O + 能力矩阵（含 evaluator / 沙箱 / 模型偏好）───
agent_io = {
    "fetcher": ([], "raw_data"),
    "analyst": (["raw_data"], "analytics"),
    "writer":  (["analytics"], "report"),
    "qa":      (["report"], "qa_report"),
}

matrix = CapabilityMatrix()
matrix.register(CapabilityRow(
    agent_id="fetcher", data_write="raw_data",
    model_preference="openai", priority=10,
))
matrix.register(CapabilityRow(
    agent_id="analyst", data_read=["raw_data"], data_write="analytics",
    model_preference="anthropic",  # 使用 llm_providers["anthropic"]
))
matrix.register(CapabilityRow(
    agent_id="writer", data_read=["analytics"], data_write="report",
))
matrix.register(CapabilityRow(
    agent_id="qa", data_read=["report"], data_write="qa_report",
    is_evaluator=True,  # True 时自动注入 EvalStage
))

# ─── 3. IntentRouter 选 Agent ───
catalog = {
    aid: AgentCapability(agent_id=aid, display_name=aid, description="")
    for aid in agent_io
}
router = IntentRouter(catalog=catalog)  # 无 LLM 时回退全量 catalog

# ─── 4. 运行（含 Interrupt + QualityGate 钩子可选）───
async def main():
    route = await router.route("生成主题分析报告")
    compiler = PlanCompiler(
        agent_io_map=agent_io,
        core_pipeline_ids={"fetcher"},
        capability_matrix=matrix,
    )
    from casa.artifact import ArtifactStore
    store = ArtifactStore(job_id="demo_001")
    orch = Orchestrator(
        compiler=compiler,
        executor=PlanExecutor(
            store=store,
            stage_runner=StageRunner(
                store=store,
                executor=MockAgentExecutor({
                    "fetcher": {"items": [1, 2, 3]},
                    "analyst": {"themes": ["A", "B"]},
                    "writer":  {"title": "Report"},
                    "qa":      {"score": 0.95},
                }),
            ),
            interrupt_ctrl=InterruptController(),  # 可选：执行中中断控制
        ),
    )
    result = await orch.run(CompileRequest(
        seed_stages=[{"agent_id": a} for a in route.agent_ids],
        policy=UsagePolicy.for_user_start(),
        intent_summary="生成主题分析报告",
    ))
    print(result.review_feedback())   # 闭环反馈
    print(result.trajectory_summary())  # 轨迹摘要

asyncio.run(main())
```

**Agent Loop（循环直到验证通过）**：

```python
from casa import AgentLoop, QualityGate, QualityGateRule

loop = AgentLoop(
    orch,
    verifier=AgentLoop._default_verifier,
    max_iterations=3,
    require_double_pass=True,
)
result = await loop.run("做一份多视角分析报告", router=router)
print(result.summary)
```

### 1.4 场景指南（场景优先）

| 你想做… | 需要初始化… |
|--------|------------|
| 自然语言 → 选 Agent → 执行 | `IntentRouter` + `PlanCompiler` + `Orchestrator.run()` |
| 结构化表单 → 校验 → 执行 | `ContractBuilder` + `ContractGate.submit()` → `Orchestrator.run()` |
| 多轮迭代直到质量达标 | `AgentLoop` 或 `Orchestrator.run_in_loop()` |
| 执行中暂停/恢复/终止 | `InterruptController` 传给 `PlanExecutor` |
| 每个 Agent 自动质量评估 | `CapabilityRow(is_evaluator=True)` → 自动 EvalStage |
| 声明式质量门（低分暂停） | `QualityGate` + `QualityGateHook` |
| 跨运行策略（配额/暂停） | `PolicyEngine` + `PolicyEnforcementHook` |
| 会话并发 + 排队 | `SessionScheduler` + `init_config(concurrency_policy="fifo")` |
| 沙箱执行 Agent | `SandboxedAgentExecutor(inner=...)` 包装领域 executor |
| Artifact 自动清理 | `Orchestrator.run(auto_cleanup=True)` 或 `LifecycleCleanupScheduler.start()` |
| analyst 用 Claude、fetcher 用 GPT | `llm_providers` + `model_preference` → `context["llm_config"]` |
| 成本追踪 | `Orchestrator.cost_breakdown()` / `debug_trace()` |
| LLM 写代码并落盘 | `CodeAgentExecutor` + `ArtifactStore.write_text()` / `read_text()` |
| 代码库 RAG 检索 | `CodebaseKnowledgeBase` + `KBRegistry` |
| 多文件交付物打包 | `RawFilesRenderer` + `DeliverableSpec` |
| Markdown 报告渲染 | `MarkdownRenderer` |
| 创意内容多版本校验 | `ValidatorFn`（见 `examples/creative_verifier.py`） |
| 沙箱挂载宿主机目录 | `SandboxedAgentExecutor` + `sandbox.mounts` |
| Git 提交前自动检查 | `examples/code_gen/git_hook.py`（Pipeline Hook 示例） |

### 1.5 CapabilityRow 标准模板（设计新 Agent 时填写）

```python
CapabilityRow(
    # ── 身份 ──
    agent_id="my_analyst",           # 必填，全局唯一
    display_name="我的分析师",
    surface="harness",               # harness | chat | api
    execution_profile="harness",     # deterministic | structured | harness | report_chapter

    # ── 许可（双许可正交）──
    tool_ids=["read_artifact", "search_knowledge"],
    data_read=["raw_data", "theme_analytics"],
    data_write="my_perspective",
    kb_read=["industry_kb"],

    # ── LLM ──
    model_preference="anthropic",    # 对应 llm_providers 中的 key
    context_limit_tokens=128000,

    # ── 评估 + 沙箱 ──
    is_evaluator=False,              # True → 自动为每个 producer 注入 EvalStage
    sandbox_memory_mb=512,
    sandbox_network="restricted",    # restricted | none | bridge
    sandbox_filesystem="read_only",  # read_only | read_write

    # ── Harness 参数 ──
    max_iterations=5,
    task_template="",
    native_fallback=False,

    # ── 分类 + 调度 ──
    role="worker",                   # dialogue | orchestrator | worker
    scope_tags=["analysis"],
    is_required=False,               # True → DAG 闭包时强制包含
    priority=100,                    # 数值越小越优先（波次内排序）
)
```

---

## 2. 模块结构

```
casa/
├── __init__.py          # 统一导出
├── _version.py          # 单源版本号
├── config.py            # 全局配置
├── contract/            # Contract 子包（models / builder / validator）
├── artifact/            # Artifact 子包（store / backend / dag）
├── scope/               # Scope 子包（ref / datastore / catalog）
├── authority/           # Authority 子包（grants / capability / store / resolver / tools）
├── orchestration/       # 编排子包（compile / execute / plan_executor / 门面 …）
├── scheduler/           # 调度子包（backend / session / backends_remote）
├── _exports.py          # 顶层 `from casa import X` 懒加载表
├── observability.py     # 可观测性
├── audit.py             # 审计追踪
├── events.py            # EventBus
├── knowledge.py         # 知识库（含 CodebaseKnowledgeBase）
├── recovery.py          # 恢复链
├── hooks.py             # Pipeline Hook
├── deliverable.py       # 交付物渲染器
├── config_loader.py     # 配置加载
├── tenant.py            # 多租户
├── api.py               # HTTP API（可选 FastAPI）
├── schema_registry.py   # Schema 注册表
├── otel.py              # OTel 桥接
├── intent.py            # IntentRouter
├── interrupt.py         # 中断控制
├── cache.py             # 跨运行缓存
├── lifecycle.py         # Artifact 生命周期
├── memory.py            # AgentMemory
├── policy.py            # PolicyEngine
├── loop.py              # AgentLoop
├── cli.py               # CLI 脚手架
└── schemas/             # PG 参考 SQL
```

推荐导入路径：`from casa.orchestration import Orchestrator`；顶层 `from casa import Orchestrator` 仍可用。

设计文档与扩展指南见 [`docs/`](docs/README.md)。

**依赖关系**：

```
config.py            ← 全局配置（所有模块依赖）
_version.py          ← 独立（__init__.py 导入）
contract.py          ← 依赖 config.py, observability.py
artifact.py          ← 依赖 config.py, observability.py, audit.py, schema_registry.py
scope.py             ← 依赖 config.py, artifact.py, knowledge.py
authority/           ← 授权子包
scope/               ← 域子包子包
orchestration/       ← 编排子包（compile / execute / plan_executor / 门面 facade）
scheduler/           ← 调度子包
contract/            ← 契约子包
artifact/            ← 产物子包
events.py            ← 依赖 observability.py（独立 ABC）
knowledge.py         ← 依赖 config.py
recovery.py          ← 独立 ABC
hooks.py             ← 独立 ABC（TYPE_CHECKING 引用 orchestrator）
deliverable.py       ← 独立 ABC
config_loader.py     ← 依赖 config.py
tenant.py            ← 依赖 config.py, config_loader.py
schema_registry.py   ← 独立 ABC
otel.py              ← 依赖 events.py
api.py               ← 依赖 orchestration（可选 fastapi）
intent.py            ← 独立（LLMCall 协议）
interrupt.py         ← 独立（asyncio.Event）
cache.py             ← 独立 ABC
lifecycle.py         ← 独立（无 CASA 内部依赖）
observability.py     ← 独立（零外部依赖）
audit.py             ← 依赖 observability.py, events.py
cli.py               ← 独立
```

---

## 3. 四支柱详解

### 3.1 Contract 契约

**职责**：跨层唯一语义源——结构化、可校验、不可被用户口头覆盖。

**链路**：
```
用户输入 → 对话采集 → build Contract → validate → 持久化 → submit Run
```

**核心类**：

| 类 | 用途 |
|---|---|
| `Contract` | 顶层容器，含 deliverable / required / context / preferences |
| `BaseDeliverable` | 交付物规格基类（领域项目继承） |
| `BaseRequired` | 任务必需参数基类（领域项目继承） |
| `ContractGate` | 采集 → 校验 → 提交的唯一入口 |
| `ContractValidator` | 领域校验器接口 |
| `SchemaContractValidator` | 基于字段声明的内建校验器 |
| `ContractMaterializer` | 将 Contract 物化为 Worker 可读的 artifact |
| `RunRequest` | Gate 校验通过后产出的提交请求 |

**使用方式**：

```python
from dataclasses import dataclass, field
from casa import Contract, BaseRequired, BaseDeliverable, ContractGate
from casa.contract import DeliverableType

# 1. 领域项目继承基类
@dataclass(kw_only=True)
class AnalysisRequired(BaseRequired):
    subject_ids: list[str] = field(default_factory=list)
    focus_id: str = ""

    def to_dict(self):
        return {"mode": self.mode, "subject_ids": self.subject_ids, "focus_id": self.focus_id}

# 2. 构建 Contract
contract = Contract(
    deliverable=BaseDeliverable(type=DeliverableType.FULL.value),
    required=AnalysisRequired(mode="compare", subject_ids=["item-001", "item-002"], focus_id="item-001"),
    session_id="s_001",
    user_id="u_001",
    intent_summary="对比分析两个对象的多视角报告",
)

# 3. 提交（validate 内建于 Gate）
gate = ContractGate()
run_req = gate.submit(contract, idempotency_key="key_001")
print(run_req.run_id)  # "run_a1b2c3d4"
```

**关键原则**：

- 编排层只读 `contract.to_dict()`，不读用户原话
- Worker 只读 `ContractMaterializer.materialize(contract)` 物化副本
- `validate()` 在提交前强制执行；口头参数无效

---

### 3.2 Artifact 产物

**职责**：Agent 间唯一通信媒介——数据流（dataflow）而非传话。

**核心类**：

| 类 | 用途 |
|---|---|
| `ArtifactDefinition` | 注册在词典中的一个产物类型 |
| `ArtifactDictionary` | 全局产物注册表 |
| `ArtifactStore` | 统一读写门面（facade，支持 local / MinIO / S3） |
| `ArtifactDAG` | 从 I/O 声明推导依赖图 + 波次划分 |
| `ArtifactSchemaValidator` | 基于 JSON Schema 的输出校验 |
| `ArtifactBackend` | 存储后端抽象接口 |

**使用方式**：

```python
from casa import ArtifactStore, ArtifactDAG

# 读写 artifact
store = ArtifactStore(job_id="j001")
store.init_plan("plan_001")
store.write("theme_analytics", {"themes": [...]})
data = store.read("theme_analytics")

# 幂等 skip
if store.exists("theme_analytics"):
    print("skip: already done")

# DAG 推导
dag = ArtifactDAG.from_declarations({
    "intel_a": ([], "raw_data"),
    "analyst": (["raw_data"], "analytics"),
    "writer":  (["analytics"], "report"),
})
stages = dag.compute_dependencies({"intel_a", "analyst", "writer"})
for s in stages:
    print(f"{s['agent_id']} depends_on={s['depends_on']}")

# 波次并行
waves = dag.partition_waves(stages)
for i, wave in enumerate(waves):
    print(f"Wave {i}: {[s['agent_id'] for s in wave]}")
```

**切换存储后端**：

```python
# 默认：本地文件
init_config(artifact_storage_backend="local")

# MinIO / S3：实现 ArtifactBackend 接口并注册
from casa import register_backend

class S3ArtifactBackend(ArtifactBackend):
    # ... 实现 write / read / list_keys 等方法

register_backend("s3", S3ArtifactBackend())
init_config(artifact_storage_backend="s3", s3_bucket="my-bucket")
```

---

### 3.3 Scope 域

**职责**：统一寻址 + 数据隔离边界。

**ref_id 命名规范**：

```
job:{job_id}:artifact:{kind}
session:{sessionId}:doc:{docName}
session:{sessionId}:intake:{field}
user:{userId}:kb:{docId}
global:knowledge:{path}
```

**核心类**：

| 类 | 用途 |
|---|---|
| `RefID` | 不可变的强类型 ref_id，工厂方法保证命名一致 |
| `ParsedRef` | 解析后的 ref_id 结构体 |
| `DataStore` | 统一读写 + 归属校验门面（facade） |
| `RefCatalog` | 当前可见 ref 列表（供 Agent 发现数据） |
| `DataStoreAccessError` | 访问被拒绝异常 |

**使用方式**：

```python
from casa import RefID, DataStore, RefCatalog

# 使用工厂方法（禁止字符串拼接）
ref = RefID.job_artifact("j001", "theme_analytics")
print(str(ref))  # "job:j001:artifact:theme_analytics"
print(ref.scope)  # "job"

# DataStore 读写（带归属校验）
ds = DataStore(
    agent_id="my_worker",
    job_id="j001",
    session_id="s001",
    artifact_store=store,
    data_grants_read=["theme_analytics"],
    data_grants_write="my_perspective",
)

# 读
data = ds.resolve_read(RefID.job_artifact("j001", "theme_analytics"))

# 写
ds.resolve_write(RefID.job_artifact("j001", "my_perspective"), {"result": "done"})

# 裸 key 被拒绝
ds.resolve_read(RefID("theme_analytics"))  # → DataStoreAccessError

# RefCatalog：当前可见 ref 列表
catalog = RefCatalog.build(
    session_id="s001",
    job_id="j001",
    artifact_store=store,
)
for entry in catalog.refs:
    print(entry["ref_id"], entry["kind"])
```

---

### 3.4 Authority 授权

**职责**：Agent 能做什么 = 工具许可 ∩ 数据许可。

**核心类**：

| 类 | 用途 |
|---|---|
| `CapabilityRow` | 一个 Agent 的许可行（填这张表 = 设计 Agent） |
| `CapabilityMatrix` | 全部 Agent 的许可矩阵 |
| `AuthorityResolver` | 合并代码默认（Code Default）与数据库覆盖（DB Override） |
| `ToolContext` | 工具运行时上下文 |
| `ToolHandler` | 工具处理器接口 |
| `ToolRegistry` | 工具注册表 |
| `GrantStore` | 许可持久化抽象 |
| `InMemoryGrantStore` | 内存实现（开发/测试用） |

**Capability Matrix 表格模板**（设计新 Agent 时填写）：

| Agent | 工具许可 | 数据读 | 数据写 | Surface |
|---|---|---|---|---|
| dialogue_intake | 对话工具集 | session/user/global | session | dialogue |
| my_analyst | read_artifact, search_knowledge | raw_data, theme_analytics | my_perspective | harness |
| report_module | 无（structured） | theme_analytics, my_perspective | report_ch_xxx | deterministic |

**使用方式**：

```python
from casa import CapabilityMatrix, CapabilityRow, AuthorityResolver

# 1. 注册
matrix = CapabilityMatrix()
matrix.register(CapabilityRow(
    agent_id="my_analyst",
    display_name="我的分析师",
    surface="harness",
    tool_ids=["read_artifact", "search_knowledge"],
    data_read=["raw_data", "theme_analytics"],
    data_write="my_perspective",
    max_iterations=5,
))

# 2. 查询
tools = matrix.tool_ids_for("my_analyst")
data = matrix.data_grants_for("my_analyst")

# 3. 校验
matrix.check_tool_grant("my_analyst", "read_artifact")       # (True, "")
matrix.check_data_read_grant("my_analyst", "report_content") # (False, "数据 report_content 不在...")

# 4. AuthorityResolver（代码默认与 DB 覆盖合并）
resolver = AuthorityResolver(matrix=matrix, grant_store=InMemoryGrantStore())
tools = resolver.resolve_tools("my_analyst")
data = resolver.resolve_data_grants("my_analyst")

# 5. 统一校验
ok, err = resolver.check_access(
    "my_analyst",
    tool_id="read_artifact",
    artifact_read="raw_data",
)
```

---

## 4. 编排层

**三层生命周期**：

```
编译时 Compile → 运行时 Normalize → 执行时 Execute
(preset + IO图)  (护栏 + 裁剪)      (DAG 波次并行)
```

**核心类**：

| 类 | 用途 |
|---|---|
| `PlanCompiler` | 确定性编译：preset + IO 图 → stages + depends_on |
| `PlanNormalizer` | 护栏：补 core pipeline、剔禁用 agent、修 depends_on |
| `StageRunner` | 单 stage 执行 + 容错链（支持 `ValidatorFn` 校验） |
| `PlanExecutor` | 波次并行执行 |
| `Orchestrator` | 编排器入口：compile → normalize → execute |
| `SimpleAgentExecutor` | 领域 handler 映射（`dict[agent_id, fn]`） |
| `CodeAgentExecutor` | LLM 驱动写代码，配合 `write_text` 落盘 |
| `SandboxedAgentExecutor` | Docker 沙箱包装内层 executor（支持 `sandbox.mounts`） |

**容错链**：

```
简单重试 ×2 → 新 LLM 会话重试 ×1 → ReplanHandler 生成应对措施 → 失败上报
```

**使用方式**：

```python
from casa import PlanCompiler, PlanExecutor, StageRunner, Orchestrator
from casa.orchestration import CompileRequest, UsagePolicy, PlanNormalizer, Preset

# 编译
compiler = PlanCompiler(
    agent_io_map=agent_io,
    core_pipeline_ids={"intel_a", "intel_b"},
    presets={
        "full": Preset(preset_id="full", selected_agent_ids=["intel_a", "analyst", "writer"]),
    },
)

result = compiler.compile(CompileRequest(
    preset_id="full",
    deliverable_type="full",
    policy=UsagePolicy.for_user_start(),
))

for s in result.plan.stages:
    print(f"{s.stage_id} ← depends: {s.depends_on}")

# 查看波次
from casa import ArtifactDAG
dag = ArtifactDAG.from_declarations(agent_io)
waves = dag.partition_waves([s.to_dict() for s in result.plan.stages])
for i, wave in enumerate(waves):
    print(f"Wave {i}: {[s['agent_id'] for s in wave]}")

# 执行
store = ArtifactStore(job_id="j001")
store.init_plan("plan_001")

stage_runner = StageRunner(
    store=store,
    executor=SimpleAgentExecutor(handlers),
)

executor = PlanExecutor(store=store, stage_runner=stage_runner)

# 完整编排
orch = Orchestrator(compiler=compiler, executor=executor)
await orch.run(CompileRequest(preset_id="full"))
```

---

## 5. 对接指南

### 5.1 从零对接（新项目）

```
1. 复制 casa/ 到项目
2. 继承 BaseRequired / BaseDeliverable（添加领域字段）
3. 声明 agent_io_map（Agent 的 input/output artifact kind）
4. 填写 CapabilityMatrix（每个 Agent 的工具许可 + 数据许可）
5. 实现 SimpleAgentExecutor 处理函数（handlers，每个 Agent 的执行函数）
6. 配置 Preset（用户可选的分析方案）
7. 初始化 Orchestrator 并调用 orch.run()
```

### 5.2 从现有内联实现迁移

若项目已有一套分散的编排、数据访问与授权逻辑，可按以下步骤收敛到 `casa` 模块：

```python
# ---- 迁移前（分散的内联实现）----
from myapp.analysis import AnalysisRequest
from myapp.data import DataStore, check_data_grant
from myapp.plan import CompileRequest, compile_plan
# ...

# ---- 迁移后（使用 casa 模块）----
from casa import (
    Contract, BaseRequired, BaseDeliverable,
    ArtifactStore, DataStore, RefID,
    CapabilityMatrix, AuthorityResolver,
    PlanCompiler, Orchestrator,
)

# 1. 将业务请求建模为 Contract
@dataclass(kw_only=True)
class AnalysisRequired(BaseRequired):
    subject_ids: list[str] = field(default_factory=list)
    focus_id: str = ""

class AnalysisContract(Contract):
    required: AnalysisRequired

    def _validate_domain(self, errors):
        if len(self.required.subject_ids) < 2:
            errors.append("需要至少 2 个分析对象")
        if self.required.mode not in ("single", "compare"):
            errors.append("mode 须为 single 或 compare")

# 2. 替换 DataStore（接口兼容）
ds = DataStore(
    agent_id="perspective_analyst",
    job_id=job_id,
    artifact_store=store,
    data_grants_read=["source_corpus", "feature_analytics"],
    data_grants_write="perspective_output",
    session_reader=session_reader,
    user_kb_reader=user_kb_reader,
)

# 3. 替换授权校验
resolver = AuthorityResolver(matrix=capability_matrix, grant_store=pg_grant_store)
ok, err = resolver.check_access(
    "perspective_analyst",
    tool_id="read_artifact",
    artifact_read="feature_analytics",
)

# 4. 替换编译器
compiler = PlanCompiler(
    agent_io_map=agent_io_map,
    core_pipeline_ids={"fetcher", "processor", "analyzer", "assembler"},
    presets=presets,
)
```

### 5.3 LLM Gateway 对接

CASA 本身不内置 LLM 调用。领域项目通过以下接口注入：

```python
# LLM Gateway 抽象——领域项目实现
class MyLLMGateway:
    async def structured(self, system_prompt, user_message, **kwargs) -> dict:
        """结构化输出（用于 structured / report_chapter profile）"""
        ...

    async def chat_completion(self, messages, tools, **kwargs) -> dict:
        """Chat completion（用于 harness tool loop）"""
        ...

# 注入到 StageRunner
stage_runner = StageRunner(
    store=store,
    executor=MyAgentExecutor(llm=MyLLMGateway()),
)
```

### 5.4 存储后端对接

```python
from casa import ArtifactBackend, register_backend

class MyS3Backend(ArtifactBackend):
    def write(self, storage_key, data, plan_dir, artifact_kind):
        # 用 boto3 写 S3
        ...

    def read(self, storage_key, plan_dir, artifact_kind):
        # 用 boto3 读 S3
        ...

    # ... 其他方法

register_backend("s3", MyS3Backend())
init_config(artifact_storage_backend="s3", s3_bucket="my-bucket")
```

---

## 6. 扩展示例

### 6.1 自定义交付物类型

```python
from enum import Enum

class MyDeliverableType(str, Enum):
    FULL_REPORT = "full_report"
    INSIGHTS = "insights"
    PATCH = "patch"

# 在 Contract 中引用
contract = Contract(
    deliverable=BaseDeliverable(type=MyDeliverableType.FULL_REPORT.value),
    ...
)
```

### 6.2 四层流水线模板

```python
agent_io = {
    # Layer 1: 采集/摄入（并行）
    "intel_a":       ([],              "raw_data_a"),
    "intel_b":       ([],              "raw_data_b"),
    # Layer 2: 处理/特征（串行）
    "processor":     (["raw_data_a", "raw_data_b"], "processed_corpus"),
    # Layer 3: 分析/视角（并行）
    "analyst_x":     (["processed_corpus"], "perspective_x"),
    "analyst_y":     (["processed_corpus"], "perspective_y"),
    "analyst_z":     (["processed_corpus"], "perspective_z"),
    # Layer 4: 交付/组装（并行模块 → 串行组装 → 串行 QA）
    "module_a":      (["perspective_x"], "report_ch_a"),
    "module_b":      (["perspective_y"], "report_ch_b"),
    "module_c":      (["perspective_z"], "report_ch_c"),
    "assembler":     (["report_ch_a", "report_ch_b", "report_ch_c"], "report_content"),
    "qa":            (["report_content"], "qa_report"),
}

dag = ArtifactDAG.from_declarations(agent_io)
waves = dag.partition_waves(dag.compute_dependencies(set(agent_io.keys())))
# Wave 0: [intel_a, intel_b]     ← 并行
# Wave 1: [processor]             ← 串行
# Wave 2: [analyst_x, analyst_y, analyst_z]  ← 并行
# Wave 3: [module_a, module_b, module_c]     ← 并行
# Wave 4: [assembler]             ← 串行
# Wave 5: [qa]                    ← 串行
```

### 6.3 仓库内场景示例

| 目录 / 文件 | 说明 |
|------------|------|
| `examples/minimal/` | 最小 4-Agent 流水线 |
| `examples/rag_qa/` | RAG 问答 |
| `examples/multi_perspective/` | 多视角并行分析 |
| `examples/kb_rag/` | 知识库驱动 RAG |
| `examples/code_gen/` | 代码生成流水线 + `GitHook` |
| `examples/creative_verifier.py` | 创意写作 + `ValidatorFn` 多版本校验 |

各示例目录含独立 `README.md` 与 `run.py`（或等效入口）。

---

## 7. 配置参考

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CASA_ARTIFACT_STORAGE_BACKEND` | `local` | 存储后端 (local / minio / s3) |
| `CASA_ARTIFACT_BASE_DIR` | `casa_jobs` | artifact 本地目录 |
| `CASA_S3_ENDPOINT` | — | S3/MinIO 服务端点 |
| `CASA_S3_ACCESS_KEY` | — | S3 访问密钥 |
| `CASA_S3_SECRET_KEY` | — | S3 密钥 |
| `CASA_S3_BUCKET` | `casa-artifacts` | S3 存储桶 |
| `CASA_LLM_API_KEY` | — | LLM API 密钥 |
| `CASA_LLM_DEFAULT_MODEL` | — | 默认模型 |
| `CASA_LLM_DEFAULT_PROVIDER` | `openai` | 默认提供方（provider） |
| `CASA_LLM_BASE_URL` | — | 自定义基础 URL |
| `CASA_LLM_TEMPERATURE` | `0.2` | LLM 温度（temperature） |
| `CASA_LLM_MAX_TOKENS` | `4000` | LLM 最大 token 数 |
| `CASA_LLM_TIMEOUT` | `60.0` | LLM 超时（秒） |
| `CASA_MAX_EVAL_STAGES_PER_PLAN` | `50` | 单 plan 最大评估 stage 数 |
| `CASA_AUTO_SKIP_DEADLOCKED_STAGES` | `false` | 死锁时自动跳过待处理 stage |
| `CASA_LIFECYCLE_AUTO_CLEANUP` | `false` | 全局生命周期自动清理开关 |
| `CASA_LIFECYCLE_CLEANUP_INTERVAL` | `3600` | 后台清理扫描间隔（秒） |
| `CASA_AUTO_REPLAN_ON_DEADLOCK` | `false` | Plan 死锁时自动重新编译 |
| `CASA_ORCHESTRATOR_LLM_ENABLED` | `false` | LLM 编排开关 |
| `CASA_STAGE_SIMPLE_RETRIES` | `2` | Stage 简单重试次数 |
| `CASA_STAGE_FRESH_SESSION_RETRIES` | `1` | 新会话重试次数 |
| `CASA_MAX_PARALLEL_PER_SESSION` | `4` | 单会话最大并发 Worker |
| `CASA_CONCURRENCY_POLICY` | `reject` | 并发策略 (reject / fifo) |
| `CASA_SCHEDULER_BACKEND` | `memory` | 调度状态后端 |
| `CASA_REDIS_URL` | — | Redis URL（分布式必需） |
| `CASA_DEBUG` | `false` | 调试模式 |

### 代码配置

```python
from casa import init_config, LLMProviderConfig, get_config

init_config(
    artifact_base_dir="my_jobs",
    llm_api_key="sk-xxx",              # 全局默认（openai）
    llm_default_provider="openai",
    llm_providers={
        "anthropic": LLMProviderConfig(
            provider="anthropic",
            api_key="sk-ant-xxx",
            base_url="https://api.anthropic.com",
            default_model="claude-sonnet-4-6",
        ),
    },
    stage_simple_retries=3,
    max_parallel_per_session=2,
    max_eval_stages_per_plan=20,
    debug=True,
)

# 领域 AgentExecutor 按 CapabilityRow.model_preference 解析凭据：
cfg = get_config()
anthropic = cfg.resolve_llm_provider("anthropic")
```

### 生命周期后台清理（Lifecycle）

```python
from casa import ArtifactLifecycleManager, LifecycleCleanupScheduler, RetentionTier

mgr = ArtifactLifecycleManager()
mgr.register_kind("scratch", RetentionTier.EPHEMERAL)

scheduler = LifecycleCleanupScheduler(mgr, interval_seconds=3600)
scheduler.start()   # 后台守护线程，按间隔扫描 artifact_base_dir
```

### 配置监视器热重载（ConfigWatcher）

```python
from casa.config_loader import ConfigWatcher

watcher = ConfigWatcher("casa.toml", mode="auto", poll_seconds=5.0)
watcher.start()  # auto：有 watchdog 时用 inotify/fsevents，否则轮询（poll）
# pip install 'casa-frame[watch]'
```

### 产物备份 / 恢复（Artifact）

```python
from casa import ArtifactBackupManager, ArtifactStore

mgr = ArtifactBackupManager()
store = ArtifactStore("j1")
store.init_plan("p1")
mgr.backup_plan(store, "/var/backups/casa")
mgr.restore_plan(store, "/var/backups/casa")  # 灾难恢复
```

### Redis 调度器（多副本）

```python
from casa import RedisSchedulerBackend, SessionScheduler, set_default_scheduler

set_default_scheduler(SessionScheduler(backend=RedisSchedulerBackend("redis://localhost:6379/0")))
# pip install 'casa-frame[redis]'
```

---

## 8. 可观测性

### 8.1 结构化日志与 RunContext

```python
from casa import configure_casa_logging, run_context, Orchestrator

configure_casa_logging()

# Orchestrator.run 自动绑定 run_id / session_id / plan_id
result = await orch.run(
    CompileRequest(preset_id="full"),
    run_id=run_req.run_id,
    session_id="s_001",
    job_id="j_001",
)
```

所有 `casa.*` logger 输出自动携带 RunContext 字段（`run_id`, `session_id`, `plan_id` 等）。

### 8.2 指标（MetricsSink）

```python
from casa import InMemoryMetricsSink, set_metrics_sink, get_metrics_sink

sink = InMemoryMetricsSink()
set_metrics_sink(sink)

await orch.run(...)

for rec in sink.snapshot():
    print(rec["name"], rec["value"], rec["tags"])
```

内置指标：`stage.duration_ms`、`artifact.size_bytes`、`scheduler.slots.active`、`contract.submit`。

### 8.3 健康检查

```python
health = orch.health_check()
# {"status": "ok", "artifact_store": {...}, "scheduler": {...}, "zombie_candidates": [...]}
```

### 8.4 审计追踪（AuditSink）

```python
from casa import InMemoryAuditSink, set_audit_sink

set_audit_sink(InMemoryAuditSink())
# Run 状态转换、artifact 写入、stage 完成自动发射 audit 事件
```

详见 [EXTENDING.md](EXTENDING.md)。

### 交付物自动渲染

注册 `DeliverableSpec` 后，可在 `Orchestrator.run()` 末尾自动渲染：

```python
from casa import DeliverableRegistry, DeliverableSpec, get_deliverable_registry

get_deliverable_registry().register(
    DeliverableSpec(deliverable_id="full", label="Report", sources=["analytics"]),
)
result = await orch.run(CompileRequest(deliverable_type="full"), auto_render=True)
# result.deliverable_output 含渲染结果与写入路径
```

或设置环境变量 `CASA_AUTO_RENDER_DELIVERABLE=true`。

---

## 9. 扩展生态

| 模块 | 职责 |
|------|------|
| `events` | EventBus 发布/订阅 |
| `knowledge` | KBRegistry + KnowledgeBase |
| `recovery` | RecoveryChain 可插拔重试 |
| `hooks` | Pipeline 生命周期扩展点 |
| `deliverable` | DeliverableRegistry + Renderer |
| `config_loader` | YAML/TOML 配置档案（profiles） |
| `tenant` | 多租户配额 |
| `api` | FastAPI 路由（可选；**默认无鉴权**，公网前须自带认证） |
| `schema_registry` | Schema 版本管理 |
| `otel` | OpenTelemetry 桥接（可选） |

---

## 10. 架构总图

```
                    ┌─────────────────────────────────┐
                    │          CASA 四支柱             │
                    │                                 │
                    │  C: Contract   — 跨层语义源      │
                    │  A: Artifact   — Agent 间媒介    │
                    │  S: Scope      — 数据隔离边界    │
                    │  A: Authority  — 双许可正交      │
                    └─────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
   ┌────▼─────┐              ┌──────▼──────┐             ┌──────▼──────┐
   │ 控制平面  │              │  执行平面    │             │  数据平面    │
   │          │  下发 plan   │             │   读写      │             │
   │ Compiler │──────────────▶  Workers    │────────────▶│ ArtifactStore│
   │ Normalizer│             │             │             │ DataStore   │
   │          │              │  Harness    │             │ RefCatalog  │
   │ 只调度   │              │  Structured │             │             │
   │ 不分析   │              │  Report     │             │ 只存取      │
   └──────────┘              └─────────────┘             │ 不决策      │
         │                                               └─────────────┘
         │ 只读状态                                              ▲
         └──────────────────────────────────────────────────────┘

  对话 Agent ──→ build Contract ──→ ContractGate ──→ submit Run
                    │
                    └──→ 编排 Agent（LLM 可选启用）──→ normalize 兜底

  每条链路：工具许可 × 数据许可 = 正交取交集
```

---

## 许可

**双许可**

- **社区**：GNU Affero General Public License v3.0 或更高版本 — 详见 [LICENSE](LICENSE)
- **商业 / 企业版（Enterprise）**：闭源集成或无法遵守 AGPL 时，请联系开发者获取商业许可：`surmusz@gmail.com`
