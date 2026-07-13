# CASA 扩展点指南

本文档说明如何实现 CASA 各 ABC 的**合格后端**。

## 产物后端（ArtifactBackend）

必须实现：

| 方法 | 要求 |
|------|------|
| `write` | 原子写入（mkstemp + rename）；路径消毒 |
| `read` | 不存在返回 None；JSON bomb 大小限制 |
| `list_keys` | 列出 plan_dir 下 artifact kind |
| `exists` | O(1) 存在检查，避免 read 双 IO |
| `delete` | 幂等删除 |
| `write_deliverable_file` / `read_deliverable_file` | 终态交付物文件；支持 `tenant_id` 路径前缀 |
| `write_report` / `read_report` | **已弃用**，ABC 提供默认实现（委托上述方法）；自定义后端无需覆盖 |

注册：`register_backend("s3", MyS3Backend())`

## 调度后端（SchedulerBackend）

必须实现：

| 方法 | 要求 |
|------|------|
| `try_acquire_slot` | **原子**：slot 分配与 cap 检查不可出现竞态（TOCTOU） |
| `save_run` | 与 slot 分配同一事务；覆写 `atomic_accept_run()` 或 Redis Lua |
| `atomic_accept_run` | **推荐**：一步完成 slot + save + heartbeat；不支持返回 `None` |
| `get_active` | **必须在 ABC 中实现** — `release()` 依赖此方法 |
| `list_queued_runs` | FIFO 出队只取 `status=queued` |
| `find_zombies` | 只返回 `accepted`/`running` 且心跳超时 run |
| `health_snapshot` | 返回 active/queued 计数 |

隐含契约：`SessionScheduler.release()` 调用 `get_active()` — 自定义 backend 不可省略。

## 许可存储（GrantStore）

持久化 Agent 工具/数据许可覆盖（override）。`AuthorityResolver` 合并代码默认（Code Default）与数据库覆盖（DB Override）。

**空值与删除语义**

| 场景 | `has_*_grant_config` | 解析结果 |
|------|----------------------|----------|
| 从未写入 store | `false` | CapabilityMatrix 代码默认 |
| `save_data_grant(read=[], write="")` | `true` | 显式空 data grant（拒绝读写） |
| InMemory：agent 键存在但工具映射（tool map）为空 | `true` | 显式空 tool 列表 |
| `delete_tool_grant` 删光最后一个 tool | `false` | 回退代码默认（撤销 DB 覆盖） |

PostgreSQL：`grant_tool_agents` 表记录「曾配置过 tool grants」；删光 `grant_tools` 行时会同步清除该标记。若需「零 tool 但仍视为 DB 覆盖」，须保留 `grant_tool_agents` 行且无 `grant_tools` 行（当前无单独 API，可通过先 `save_tool_grant` 再删行并保留 marker 实现）。

变更 grant 后调用 `resolver.invalidate_cache(agent_id)`。

## Agent 执行器（AgentExecutor）

```python
async def execute(self, agent_id: str, context: dict) -> dict:
    # 必须返回 dict（artifact payload）
```

## 契约校验器（ContractValidator）

返回 `list[str]` 错误列表（空 = 通过），不抛异常。

## 契约迁移器（ContractMigrator）

```python
from casa import register_contract_migrator

def v1_to_v2(data: dict) -> dict:
    data = dict(data)
    data["version"] = 2
    data["new_field"] = data.get("old_field", "")
    return data

register_contract_migrator(1, 2, v1_to_v2)
```

## 指标接收器（MetricsSink）

```python
class PrometheusSink(MetricsSink):
    def record(self, name, value, tags=None):
        ...
```

## 审计接收器（AuditSink）

默认 `NullAuditSink`（零开销）。生产环境注入 PG/ES 实现。

事件类型：`run.status_changed`、`artifact.written`、`stage.completed`。

## 测试隔离

```python
from casa import reset_config, reset_metrics_sink, reset_audit_sink, reset_scheduler
```

使用 `override_config()` context manager 做测试隔离。

## 知识库（KnowledgeBase）

实现 `search` / `get` / `list_entries`，注册到 `KBRegistry`：

```python
from casa import KBRegistry, InMemoryKnowledgeBase, KBEntry

registry = KBRegistry()
registry.register(InMemoryKnowledgeBase("docs", scope="global"))
```

**访问控制**：在 Agent 级通过 `CapabilityRow.kb_read` 或 `DataStore(kb_read=[...])` 限制可访问的 `kb_id`。
- `kb_read=None`：未指定限制，可见全部已注册 KB
- `kb_read=[]`：明确无权限，不可访问任何 KB
- `KBEntry.tags` 仅为可选元数据；框架不在条目级做权限校验

生产级向量后端（如 pgvector）实现 `KnowledgeBase` 即可，签名与 `ORDER BY embedding <=> $1 LIMIT $2` 天然契合：

```python
class PgVectorKnowledgeBase(KnowledgeBase):
    async def search(self, query, *, top_k=5, filters=None):
        vec = await self._embed(query)
        # SELECT ... ORDER BY embedding <=> $1 WHERE ... LIMIT $2
        ...
```

遗留 `user_kb_reader` / `global_kb_reader` 自动包装为回调式（callback）KB。
遗留用户 KB 的 `get()` 使用 `"{uid}:{entry_id}"` 复合键。

## 事件总线（EventBus）

```python
from casa import get_event_bus, Event

bus = get_event_bus()
sub_id = bus.subscribe("stage.*", lambda e: print(e.event_type))
bus.unsubscribe(sub_id)
```

`emit_audit` / `log_event` 自动发布到 EventBus。

## 流水线钩子（PipelineHook）

```python
from casa import HookRegistry, PipelineHook, Orchestrator

class MyHook(PipelineHook):
    async def on_stage_end(self, stage, result):
        ...

orch = Orchestrator(..., hooks=HookRegistry())
orch.hooks.register(MyHook())
```

## 恢复策略（RecoveryStrategy）

```python
from casa import RecoveryChain, SimpleRetryStrategy, StageRunner

chain = RecoveryChain([SimpleRetryStrategy(max_retries=3)])
runner = StageRunner(..., recovery_chain=chain)
```

## 交付物注册表（DeliverableRegistry）

```python
from casa import DeliverableRegistry, DeliverableSpec, UsagePolicy

reg = DeliverableRegistry()
reg.register(DeliverableSpec(deliverable_id="full", label="Report", sources=["analytics"]))
policy = UsagePolicy.from_deliverable_type("full")
```

## 配置加载器（ConfigLoader）

```python
from casa import ConfigLoader, init_config
cfg = ConfigLoader.from_yaml("casa.yaml", profile="production")
init_config(**cfg.to_dict_safe())
```

## 租户管理器（TenantManager）

```python
from casa import InMemoryTenantManager, Tenant
mgr = InMemoryTenantManager()
mgr.register(Tenant(tenant_id="acme", quotas={"max_parallel": 10}))
```

## Schema 注册表（SchemaRegistry）

```python
from casa import get_schema_registry
get_schema_registry().register("analytics", 1, {"required": ["themes"]})
```

## HTTP API（可选）

> **安全警告**：`create_router()` **默认无认证**。仅用于受信内网或本地调试；  
> 对公网暴露前必须自行叠加鉴权（API Key / OAuth / 反向代理 ACL 等）。  
> `/casa/loops`、`/casa/runs` 等会直接触发编排，切勿裸端口放行。

```python
# pip install 'casa-frame[api]'
from casa.api import create_router
app.include_router(create_router(orchestrator))
```

## 契约国际化（Contract i18n）

```python
from casa import Contract, DictContractI18nProvider

i18n = DictContractI18nProvider({
    "contract.deliverable.type": {"zh": "交付物类型", "en": "Deliverable type"},
})
schema = Contract.schema(locale="zh", i18n=i18n)
```

## 依赖注入 vs 全局单例

- **构造函数注入**（推荐）：`Orchestrator(hooks=...)`, `StageRunner(recovery_chain=...)`

## 意图路由器（IntentRouter）

```python
from casa import IntentRouter, AgentCapability

router = IntentRouter(
    catalog={
        "fetcher": AgentCapability(agent_id="fetcher", display_name="Fetcher",
                                    description="采集原始数据"),
    },
    llm_call=my_llm_gateway,
)
result = await router.route("帮我做一份多视角分析报告")
# result.agent_ids, result.policy, result.warnings

# 或从 CapabilityMatrix 自动构建
router = IntentRouter.from_capability_matrix(matrix, agent_io_map)
```

## 中断控制器（InterruptController）

```python
from casa import InterruptController

ctrl = InterruptController()
ctrl.pause("用户请求暂停", after="wave")  # 或 after="stage"
ctrl.resume()
ctrl.abort("任务取消", graceful=True)
```

注入到 `Orchestrator` 和 `PlanExecutor` 后，会在波次/stage 边界自动轮询。

## 产物缓存后端（ArtifactCacheBackend）

```python
from casa import ArtifactCacheBackend, LocalArtifactCache, cache_key

# 默认本地实现
c = LocalArtifactCache("casa_cache")
# 自定义 Redis/PG 实现
class MyCache(ArtifactCacheBackend):
    def get(self, key): ...
    def put(self, key, data, metadata=None): ...
    def invalidate(self, kind): ...

runner = StageRunner(store=store, executor=exec, cache_backend=c)
```

## 产物生命周期管理器（ArtifactLifecycleManager）

```python
from casa import ArtifactLifecycleManager, RetentionTier

lm = ArtifactLifecycleManager()
lm.register_kind("raw_data", RetentionTier.EPHEMERAL)
lm.register_kind("report_content", RetentionTier.PERMANENT)
lm.cleanup_plan(store, plan_id="p1", job_id="j1", dry_run=True)
```

## Redis 调度后端（RedisSchedulerBackend，参考实现）

```python
from casa import RedisSchedulerBackend, SessionScheduler, set_default_scheduler

backend = RedisSchedulerBackend("redis://localhost:6379/0")
set_default_scheduler(SessionScheduler(backend=backend))
# submit() 自动走 Lua atomic_accept_run
```

## PG 调度后端（PgSchedulerBackend，参考实现）

```python
from casa import PgSchedulerBackend, SessionScheduler

# 先执行 casa/schemas/scheduler_pg.sql
backend = PgSchedulerBackend("postgresql://user:pass@host/db")
sched = SessionScheduler(backend=backend)
# pip install 'casa-frame[postgres]'
```

## PG 许可存储（PgGrantStore，参考实现）

```python
from casa import PgGrantStore, AuthorityResolver

# 先执行 casa/schemas/grants_pg.sql
store = PgGrantStore("postgresql://user:pass@host/db")
resolver = AuthorityResolver(matrix=matrix, grant_store=store)
```

## S3 产物后端（S3ArtifactBackend，参考实现）

```python
from casa import S3ArtifactBackend, register_backend, init_config

register_backend("s3", S3ArtifactBackend(bucket="casa-artifacts", ...))
init_config(artifact_storage_backend="s3", s3_bucket="casa-artifacts")
# pip install 'casa-frame[s3]'

# 预签名 URL（S3 扩展能力）
url = backend.presigned_url("artifacts/j1/p1/report")
```

## Agent 协调提示

```python
store.write("raw_data", data, coordination_hint={"format": "weekly"})
# 下游 context["coordination_hints"]["raw_data"]
```

### 多版本写作约定（内容创造场景）

创意工作流中，artifact 版本链通过 `coordination_hint` 传递，不污染 artifact 本体：

```python
store.write("chapter_3", {"text": "..."}, coordination_hint={
    "version": 3,
    "parent_version": 2,
    "author_agent": agent_id,
    "change_summary": "修改结尾，加强反派动机",
})

hint = store.read_coordination_hint("chapter_3")
# 下游 Agent 从 context["coordination_hints"]["chapter_3"] 读取
```

字段约定：

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | int | 当前版本号 |
| `parent_version` | int | 父版本（用于 diff / 回滚） |
| `author_agent` | str | 产出该版本的 Agent |
| `change_summary` | str | 人类可读变更摘要 |

## 多场景交付物渲染

```python
from casa import DeliverableSpec, ChapterSpec, MarkdownRenderer, RawFilesRenderer, get_deliverable_registry

reg = get_deliverable_registry()
reg.register(DeliverableSpec(
    deliverable_id="novel",
    label="小说稿",
    renderer="markdown",
    chapters=[
        ChapterSpec(chapter_id="c1", title="第一章", source_artifact="chapter_1"),
    ],
))
reg.register(DeliverableSpec(
    deliverable_id="code_bundle",
    label="代码包",
    renderer="raw_files",
    sources=["main_py", "utils_py"],
))
```

- `MarkdownRenderer` — 内容创造，按章节拼接 Markdown
- `RawFilesRenderer` — 代码生成，将 `_text_content` artifact 打包为 zip

## 纯文本 Artifact（代码场景）

```python
store.write_text("main_py", "def hello(): pass")
code = store.read_text("main_py")
```

## 自定义 stage 校验器（lint / test）

```python
def lint_validator(artifact_kind: str, data: dict) -> list[str]:
    text = data.get("_text_content", "")
    if "import *" in text:
        return ["禁止使用 import *"]
    return []

runner = StageRunner(store=store, executor=executor, schema_validator=lint_validator)
```

## 代码库知识库（CodebaseKnowledgeBase，仓库索引）

```python
from casa import CodebaseKnowledgeBase, KBRegistry

kb = CodebaseKnowledgeBase("repo", repo_path="/path/to/project")
kb.index_files(["*.py"])
hits = await kb.search("scheduler backend")
```

## 产物备份管理器（ArtifactBackupManager）

```python
from casa import ArtifactBackupManager, ArtifactStore

store = ArtifactStore("j1")
store.init_plan("p1")
store.write("report", {"ok": True})

mgr = ArtifactBackupManager()
mgr.backup_plan(store, "/var/backups/casa")
# 灾难恢复：
mgr.restore_plan(store, "/var/backups/casa")
```

---

## 贡献约束

| 规则 | 目标路径 |
|------|----------|
| 新 Executor | `casa/orchestration/executor.py` 或独立文件 |
| 新 Hook | `casa/hooks.py` |
| 新 Renderer | `casa/deliverable.py` |
| 新 Backend | `casa/artifact/backend.py` 或 `casa/scheduler/backends_remote.py` |
| 编排逻辑 | `casa/orchestration/` 子模块，**禁止**往 `facade.py` 无节制堆方法 |
| 单文件行数 | ≤ 600（`python scripts/check_module_size.py`，**无白名单**） |
| 测试命名 | `test_<module>.py`，禁止 `test_v*` / `test_phase*` |

**推荐规范（canonical）导入**：

```python
from casa.orchestration import Orchestrator, PlanCompiler, CompileRequest
from casa.artifact import ArtifactStore
from casa.contract import Contract, ContractGate
from casa.scheduler import SessionScheduler
```

顶层 `from casa import Orchestrator` 仍可用（一级 / Tier-1 导出）。

- **全局单例**：`set_event_bus()`, `get_kb_registry()` — 适合进程级替换与测试 `reset_*()`
- 优先级：构造函数参数 > 全局单例
