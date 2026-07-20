# ADR-0001 · Cursor API 集成（以报告/产物审查为主）

> 状态：**Accepted · 2026-07-20** · 日期：2026-07-20 · 适用仓：`casa-frame`（库）
> 对齐基线：`casa_agent/01-decisions.md`（vlepontas 平台全局 ADR 1–57）
> 本 ADR 只设计**库层**集成；平台层装配（lease / epoch / fencing / 计费 / tenant_policy）以全局 ADR 为准，本文仅声明约束接口。

---

## 1. 背景

为 casa-frame 增加一项可选能力：**主推**调用 Cursor API 对产物做**独立审查**；生成能力仅为可选 extra，**非主推、非本 ADR 主验收路径**。报告内容仍由主 provider 生成，Cursor 以独立 `agent_id` 审查产物（满足 builder≠judge）；审查执行落在 worker 侧显式 review stage（见 §2.2），**不等同于** ADR-48「终段 evaluator stage」字面（见 M1 / §10）。

映射到框架现有扩展缝，**不新增核心抽象**：

| 能力 | 定位 | casa-frame 缝 | 形态 |
|------|------|---------------|------|
| 审查内容 | **核心（主推、主验收）** | 显式 review **stage** → worker `AgentExecutor`（`agent_id=cursor-reviewer`） | `CursorAgentExecutor`（审查用 read-only 配置） |
| 审查排程信号 | 可选、非 LLM | `PipelineHook.on_stage_end`（过滤/是否排 review stage） | `CursorReviewHook`（**禁止**在 orch 调 SDK） |
| 生成内容 | 可选 extra，**非主验收** | 同上 `AgentExecutor` | `CursorAgentExecutor`（写路径）+ `CursorContentGenerator` |

Cursor SDK（`cursor-sdk` Python / `@cursor/sdk` TS）的 `Agent.prompt()` / `Agent.create().send()` **仅**在 worker 进程内经 `CursorAgentExecutor`（或薄 helper）调用，不在 orch 持密钥或调 API。

## 2. 决策

### 2.1 落点：可选 extra，不耦合内核

- 代码位于 `casa/extras/cursor/`（新模块），`pyproject.toml` 增 `optional-dependencies.cursor = ["cursor-sdk==X.Y"]`（版本钉死，beta 防漂移）。
- `_exports.py` 懒加载导出 `CursorReviewHook` / `CursorAgentExecutor` / `CursorContentGenerator` / `CursorConfig`。
- **不装 extra 则零依赖**，内核仍无 LLM 客户端（对齐 ADR-8「casa-frame=库」、ADR-37「内核无 LLM 客户端=Fact」）。
- **凭证与调用仅在 worker**（ADR-37）：库**不内置、不持久化** provider 密钥；运行时由调用方（worker）将 `CURSOR_API_KEY` 注入到 config 对象，生命周期随调用方进程。orch / api **不得**持密钥，也**不得**发起 Cursor LLM 调用。本集成标注为「可选参考实现」。
- 单文件 ≤ 600 行（`scripts/check_module_size.py`），无白名单。
- **SDK public beta**：`AsyncClient.launch_bridge` / `AsyncAgent` 等方法签名以实现期 `pip install cursor-sdk` 后 `dir()/help()` 核对为准，不靠记忆；所有 cursor-sdk 调用集中在 `casa/extras/cursor/_runtime.py` 适配层，SDK 漂移只改一处。

### 2.2 核心组件（审查 = 显式 review stage + worker executor）

**核心定位（B1 方案 A）**：Cursor 审查 = Contract/DAG 中的**显式 review stage**，经 worker 的 `AgentExecutor`（`agent_id=cursor-reviewer`）执行。LLM 调用只发生在该 stage 的 worker 进程内。

**`CursorAgentExecutor(AgentExecutor)`** — **核心组件**（审查主路径；生成路径同缝但非主验收）：
- 跑在 **worker**（对齐 ADR-36：worker = 无状态 stage 执行器，`AgentExecutor` 为唯一接缝；句柄不跨 stage）。
- **审查配置（主推）**：read-only——只授 `cursor.read`；`runtime=local` + 只读 `cwd`，或 `runtime=cloud` + `repos` 且 `auto_create_pr=False`；用 `Agent.prompt(...)` one-shot（自带 dispose）或等价 async 路径。
- `execute(agent_id, context)`：取 `context["injected_prompt"]`（或平台注入的待审产物摘要）作为 Cursor prompt；按 `agent_id` 解析 model 别名；按 `CursorConfig.runtime` 选 **`runtime=local` + `cwd=…`** 或 **`runtime=cloud` + `repos=…`**；`await run.wait()` 后将 `RunResult` 映射为 artifact dict。
- prompt 强制输出 **JSON schema**：`{verdict: pass|fail|conditional, issues:[{severity,location,desc}], summary}`。JSON schema 只约束**报告形态**；安全边界仅靠 filesystem 只读挂载 + 不开 `auto_create_pr`（不依赖 JSON「硬」安全）。
- **JSON 解析容错**：解析失败 → 兜底 `verdict=conditional, summary="review_parse_failed"`，原始文本入 audit；不悬空。
- **`verdict=fail` / `conditional` 效力（M4）**：默认写 review artifact + audit；**是否 fail 当前/上游 stage** 由 Contract 与 `review_stages` 策略决定（平台可配），库不硬编码「fail 即阻断流水线」。
- reviewer 与 producer 为不同 `agent_id`（满足 builder≠judge）；**满足独立 reviewer 与确定性触发，不等同于 ADR-48 的终段 evaluator stage**——若需严格终段，Contract 须显式把 review 排为终段 stage。
- **句柄在单次 execute 内 create+dispose**，**不跨 stage 复用**（对齐 ADR-36）。`Agent.resume` 跨进程续接推迟 P2+。
- `execute_streaming` / 中断 / async / dispose：与审查同缝；流式与 cancel 见 §8。
- **生成路径（非主验收）**：同一 executor 可走写配置；篇幅从简，实现期不以生成为主验收项。见 §11。

**`CursorReviewHook(PipelineHook)`** — **可选、非 LLM 触发/过滤信号**（降级，非核心 LLM 路径）：
- 挂 `on_stage_end`，按 `CursorConfig.enabled` + `review_stages` 决定**是否排程/标记**后续 review stage（或向编排发出「需审」信号）。
- **禁止**在 hook 内、orch 进程内调用 cursor-sdk / `Agent.prompt` / 持 `CURSOR_API_KEY`。
- `enabled=false` 时 **no-op**（即使已 register）；`review_stages` 为空时不审、不排 review stage。
- 确定性触发由 casa/Contract 决定，不让 LLM 自判是否触发。

**`CursorContentGenerator`** — 一次性生成 helper，可选 extra，**非主验收**：薄封装 `Agent.prompt()`，仅用于不走完整编排的「给 prompt 出文本」；须在 worker 侧调用。

### 2.3 配置

- 复用 `LLMProviderConfig(provider="cursor", api_key, base_url="", default_model="composer-2.5")`：`api_key` 等字段为**运行时注入载体**，库不内置/不持久化密钥（见 §2.1 / §9）。`composer-2.5` 仅为未配置时的 **fallback**，非路由表硬编码（ADR-49）。
- 新增 `CursorConfig`：
  - `enabled`（bool，**默认 false**）— 见 §2.4
  - `review_stages`（`list[str]`，默认空）— 哪些 stage 产物受审；空 = 不审
  - `runtime`（`local` | `cloud`）+ 对应字段：`cwd`（local）/ `repos`（cloud）
  - `cwd_policy`（scratch / readonly / repo-subtree）
  - `mcp_servers`、`skip_reviewer_request=True`（默认）、`auto_create_pr=False`（默认）
  - `per_provider_concurrency`（**默认 unset**，见 §3）、`rate_limit`（令牌桶，advisory，**默认 unset**）
  - 可选 tenant 级覆盖字段（由平台注入，库不读 mgmt）
- model **别名**解析（对齐 ADR-49）：`simple/hard/reviewer/architect` → mgmt 可配 → cursor-sdk model id；**不硬编码 slug**。
- 运行时 `Cursor.models.list()` 校验配置的 model id 可用且账号有权限。

### 2.4 管理侧一键开关 + 审查范围可配

**库层载体**（本 ADR 约束）：

| 字段 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `CursorConfig.enabled` | bool | `false` | 总开关；`false` 时 `CursorReviewHook` no-op，且不排 Cursor review stage |
| `CursorConfig.review_stages` | `list[str]` | `[]` | 哪些 stage 的产物进入审查；空列表 = 不审 |
| `CursorConfig.fail_stage_on` | `list[str]` | `[]` | 哪些 stage 在 verdict 为 `fail`/`conditional` 时标记 stage 失败；**空 = audit-only**（仅写 review artifact + audit，不 fail stage）；对齐 ADR-58 |
| （可选）tenant 覆盖 | 由平台注入同一载体 | — | 库只提供字段与注入点，**不读 mgmt** |

**平台路径**（库声明前置条件，平台装配）：

1. mgmt **唯一写**持久策略：`tenant_policy.cursor_review.{enabled, review_stages}`（对齐 ADR-26）。
2. 经 **TenantPolicySync**（ADR-33：配置先写 redis-control，再晋升 PG `effective_version`）下发到消费端。
3. worker 读 effective policy，注入 `CursorConfig`；orch 编排只读信号（是否排 review stage），**不**持 Cursor 凭证、不调 SDK。

**一键开关** = mgmt 后台单开关写 `enabled`。  
**审查范围** = `review_stages` 配置项。

**语义**：`enabled=false` → hook no-op（即使已 register）；`review_stages` 为空 → 不审、不排 review stage。

## 3. 并发与 30 用户目标（关键）

> 全局目标 ADR-3：30 用户同时，典型 dialogue+orch+2 worker ⇒ 全局约 **60 worker 槽（W）**。数字为产品目标而非 SLA。

### 3.1 两层并发，职责分离，禁止双计数

| 层 | 职责 | 归属 | 计数器 |
|----|------|------|--------|
| **平台执行帽** | tenant active + session active + global W 三维原子 | 平台 `ExecutionLeaseGate`（ADR-9/16） | **唯一**执行计数；worker 持 lease 才调 Cursor |
| **Cursor API 保护帽** | 防止触发 Cursor 侧 429 / 限流 | worker 内 `CursorAgentExecutor` 侧 per-provider 信号量 / 令牌桶 | **advisory**；机制存在，**默认关闭（unset）** |

**铁律**：
- **不得**引入与 `ExecutionLeaseGate` 冲突的全局执行帽或「executor 级准入」（对齐 ADR-16「禁止第二 FIFO / 双 run_id」精神）。
- Cursor 侧保护帽只针对**外部 API 限流**，不是准入：`per_provider_concurrency` / `rate_limit` **默认 unset**；L0 实测前不得填默认值。
- **Norm（B2）**：`per_provider_concurrency` 触发时**只允许**等待 / 退避 / `RecoveryChain`（计为外部限流），**禁止**映射为 lease 拒绝，**禁止**在未持 lease 的路径上用该信号量做全局排队。

### 3.2 30 并发下的可观测约束

- `per_provider_concurrency` 与 `rate_limit` **默认 unset（跟随 lease）**；Cursor 实际限流未知（beta），库不应猜；**L0 实测限流后**再按实测值配。
- 若 Cursor 实测限流 < W，**应由平台层节流**（lease 维度或 worker 拉取节流）；worker 内信号量仅兜底，会牺牲 lease 利用率，非首选。
- 429 走 `RecoveryChain` 退避（尊重 `CursorAgentError.is_retryable` / `retry_after`）。
- 30 用户 × 多 stage 并发会同时存在多个 `AsyncAgent` / `AsyncClient`：**每事件循环一个 `AsyncClient`**（SDK 规则），长跑服务 shutdown 调 `close_default_client()`；不得跨事件循环复用 client。
- cloud agent 开 PR = 有副作用，30 并发下默认 `auto_create_pr=False` + `skip_reviewer_request=True`，避免误发 PR / 误告警风暴。

### 3.3 L0 早测（对齐 ADR-55 冒烟口径）

- L0 即含 **5–10 并发 real-provider 冒烟**：限流行为 / TTFT / 每 run 调用数与 duration / 失败模式。
- 报告标 `mock|real`；Cursor 不暴露 token usage，单位经济账用「每 run 调用数 × 单价 + duration」近似，**token 成本不可得**，明确标注为 Unknown。
- **经济账 Gate 口径**见 §6 / §10 / 本 Plan step 7，不在本库 ADR 闭合。

## 4. 权限与沙箱（对齐 ADR-35 / ADR-47）

Cursor agent **自带 file-edit / shell 工具** = 高副作用执行体。**casa `ToolGrant` 对 Cursor agent 是声明性 + 审计层，约束不住其内置行为**（SDK 未暴露禁用内置工具的选项）；硬约束靠 runtime 选择。

**Norm**：声明性授权失败**不得**被实现成唯一拦截；无硬挂载只读则**不得**宣称审查 read-only 安全。`ToolGrant` / `cursor.*` **不得**当作可执行硬否决。

- 新增 `CapabilityMatrix` tool_ids：`cursor.read` / `cursor.write` / `cursor.shell` / `cursor.pr`（声明性 + 审计）。
- **审查（核心场景，read-only）**：只授 `cursor.read`；`runtime=local` 时 `cwd` = **只读挂载副本/scratch**（文件系统层强制不可写，硬约束）；`runtime=cloud` 时用 cloned repo，不开 `auto_create_pr`，只取 JSON 形态报告。可证约束：副作用默认限制在隔离 VM / **不外发 PR**；**不**声称「无副作用」（cloned repo / VM 内仍可改工作区）。
- **生成（可选，非主验收）**：生产走 **cloud（隔离 VM）**；local 生成仅 dev/CI 受信环境。
- **sandbox 与 L1（M3）**：Cursor local agent 的 **`cwd` 落 L1 目录策略**（scratch 配额等）≠ 将 Cursor runtime **整进程**套入 ADR-47 L1「禁网」沙箱。Cursor runtime **需要出网**调公网 API，不适用 L1 禁网整进程模型。审查仍约束 `cwd` 只读挂载。
- **平台装配必须**经 ADR-35 wrapper（调用前验证 epoch fence；幂等键固定 `run_id#attempt#stage`）。库文档声明此前置条件；库本身无法强制平台装配。

## 5. Fencing 与幂等（对齐 ADR-32 / ADR-35）

- Cursor run 的**启动**与**终态映射**必须挂在所在 attempt 的 CAS 之下（ADR-32）：`running→terminal`、cancel、usage outbox 均以 `(run_id, attempt, status=running, lease_epoch, owner_token, lease_expires_at>now)` 为前置。
- Cursor run 按 ADR-35 工具副作用分类：
  - **只读审查**：幂等且只读 → 可自动接管 retry。
  - **local 生成（改文件）**：可观察、可补偿 → 落 `tool_effects` intent/result + 补偿 handler；幂等键 `run_id#attempt#stage`。
  - **cloud 开 PR（不可逆）**：禁止自动 retry / TTL 接管，转 `needs_manual_reconciliation`；默认关 `auto_create_pr`。
- Cursor `run.id` / `agent.agent_id` 立即记录到 audit / metrics，便于 30 并发下排障。

## 6. 计量与计费（对齐 ADR-37 / ADR-55）

- Cursor provider 凭证与 **LLM 调用**仅在 worker；orch / api 不持凭证、不调 Cursor（ADR-37）。
- Cursor 不暴露 token usage → casa 记**元数据 meter**：`{provider:"cursor", run_id, agent_id, model, status, duration, call_count}`。
- 平台计费经 `billing.usage` 事件（ADR-27）；Cursor 仅发「调用元数据」，**不发 token 成本**（不可得，标 Unknown）。
- **经济账（M2）**：库层对齐 ADR-55 的 **L0 冒烟口径**（§3.3）。**单位经济账 Gate 口径待平台 ADR 修订**（本 Plan **step 7**）；未修订前 Gate 对 Cursor provider **不可验收**。Cursor 无 token、无公开单价时，降级为「调用次数 × 套餐摊销」模型——若套餐席位制则月费摊销，若按用量则需 Cursor team usage API（不提供则只能做调用数上限管控）。
- `UsagePolicy` 按「并发 Cursor run 数 / 日调用数」限配额；token 级配额对 Cursor 无意义，不配置。

## 7. 错误与恢复

- 两类失败分别映射（SDK 陷阱 #2）：
  - `CursorAgentError`（启动失败：auth / config / network）→ casa `StageExecutionError`（启动类），走 `RecoveryChain` 退避，尊重 `is_retryable` / `retry_after`。
  - `result.status == "error"`（运行中失败）→ casa `StageExecutionError`（运行类），检查 transcript / git state，按 ADR-35 分类决定 retry / 补偿 / 手动。
- 退出码语义：启动失败 ≠ 运行失败，分别走不同 Recovery，不混用。

## 8. 流式与中断

- 流式：`execute_streaming` → `run.messages()`，assistant text block → `on_chunk`；`wait()` 不可省（否则无法判定 finished / error / cancelled）。
- 中断：`InterruptController`（ADR-39 三层取消）→ `run.cancel()`（`supports("cancel")` 判定）；worker 轮询 `casa:cancel:{run_id}` 与 Cursor run cancel 联动。

## 9. 凭证

- `CURSOR_API_KEY` 仅 worker 环境注入（ADR-37：凭证**与调用**仅在 worker）。
- 库不内置、不持久化密钥；`LLMProviderConfig.api_key` / `CursorConfig` 相关字段仅为运行时注入载体，生命周期随调用方进程。
- 生产代码**显式传 `api_key`**，不靠 ambient env（SDK 生产 best practice #7，防 30 并发下跨租户误用）。
- inline MCP servers 携密钥，**不持久化**；`Agent.resume` 需重新传入。

## 10. 与全局设计对齐表

| 全局 ADR | 本集成约束 |
|----------|-----------|
| ADR-3（30 用户 / 60 W） | §3：平台 lease 管执行帽；Cursor 保护帽默认 unset、仅 advisory |
| ADR-8（casa-frame=库） | §2.1：可选 extra，不耦合内核 |
| ADR-9/16（ExecutionLeaseGate 唯一执行计数） | §3.1：禁止第二并发帽 / 双计数；保护帽触发只允许等待/退避/RecoveryChain，禁止 lease 拒绝与未持 lease 全局排队 |
| ADR-26（mgmt 唯一写 Grant/策略） | §2.4：`tenant_policy.cursor_review.*` 由 mgmt 唯一写；库不读 mgmt |
| ADR-32（epoch fencing） | §5：Cursor run 启动/终态挂 attempt CAS |
| ADR-33（TenantPolicySync） | §2.4：enabled/review_stages 经 Redis→PG effective_version 下发；worker 注入 `CursorConfig` |
| ADR-35（外部工具副作用） | §4/§5：平台装配必须经 wrapper + epoch fence + 幂等键 `run_id#attempt#stage` |
| ADR-36（orch/worker 分解） | §2.2：审查经 worker `AgentExecutor`；句柄不跨 stage；Hook 不在 orch 调 SDK |
| ADR-37（LLM 只在 worker） | §2.1/§6/§9：凭证**与调用**仅在 worker；库不内置/不持久化密钥 |
| ADR-47（sandbox 分级） | §4：cwd 落 L1 **目录策略**；Cursor runtime 需出网，不适用 L1 禁网整进程 |
| ADR-48（builder≠judge） | §2.2：独立 `agent_id` + 确定性触发；**不等同于** ADR-48 终段 evaluator stage；严格终段须 Contract 显式排 |
| ADR-49（model 别名路由） | §2.3：别名 → mgmt 可配 → cursor id；`composer-2.5` 仅 fallback |
| ADR-55（L0 real-provider 早测） | **冒烟口径**：§3.3 对齐（5–10 并发、token Unknown）。**经济账口径**：待平台 ADR 修订（本 Plan step 7），未修订前 Gate 对 Cursor **不可验收** |

## 11. 非目标 / 推迟

- **不**在内核新增 LLM 客户端抽象（保持 ADR-37）。
- **不**把 Cursor 作为必选依赖；不装 extra 不影响他人。
- **不**主推 Cursor 生成内容；`CursorContentGenerator` 与写路径 executor 配置为可选 extra，**非本 ADR 主验收**。
- **不**由 executor 维护平台执行帽（归 `ExecutionLeaseGate`）。
- **不**在 orch / `PipelineHook` 内调 Cursor SDK。
- **不**默认开 `auto_create_pr`。
- **不**做 token 级成本核算（Cursor 不暴露）。
- L2 Docker 异质 sandbox、self-hosted cloud pool、`Agent.resume` 跨进程续接：P2+ 再评估。

## 12. 风险

1. **AGPL / 许可（实现 Gate）**：引入 `cursor-sdk` 前须确认许可（含 AGPL 风险）；**实现 Gate 单列确认**后方可合入 optional extra；optional 规避强制依赖。
2. **Cursor 限流 vs 30 并发**：L0 实测前 `per_provider_concurrency` 保持 unset；实测后按限流配。
3. **local agent 复用调用方环境**：跨租户风险，强制显式 `api_key` + 隔离 cwd。
4. **cloud 副作用**：默认关 PR；不声称无副作用。
5. **async 客户端泄漏**：`async with` + `finally close` 不可省。
6. **审查 JSON 输出可靠性**：解析容错 + 校验失败兜底 `conditional`（§2.2）。
7. **审查确定性**：排程/触发由 casa（enabled + review_stages + Contract）决定；LLM 只在 worker review stage 产审查内容。
8. **SDK public beta 漂移**：适配层 `_runtime.py` 隔离，版本钉死，实现期端核签名。

## 13. 后续

- 草案批准后 → 进 CASA Contract（`casa模式`）实现 `casa[cursor]`，先产 Contract 再动业务代码。
- 实现顺序建议：`CursorAgentExecutor`（审查 read-only 配置，核心）+ 可选 `CursorReviewHook`（非 LLM 排程信号）+ 集成测试（async / worker 路径冒烟）→ 可选生成 helper → L0 real-provider 早测。
- 平台装配（lease/epoch/fencing/计费 wrapper、tenant_policy 开关）由 vlepontas 在 worker/mgmt 层落地。
- **ADR-55 经济账验收口径修订**由平台 ADR 处理，跟踪本 Plan **step 7**；未完成前不得宣称 Cursor provider Gate 可验收。
