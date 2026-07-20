# Cursor 审查集成 · CASA 框架影响分析

> 日期：2026-07-20 · Plan `20260720-cursor-impl` Step 8  
> 范围：库层 `casa[cursor]` 可选 extra + 平台 ADR 对齐（ADR-55 修订 / ADR-58 新增）  
> 依据：库 [ADR-0001](adr/0001-cursor-api-integration.md)、平台 `casa_agent/01-decisions.md`、实现 `casa/extras/cursor/**`、step 3/6 产物与 step 4/5 review 吸收结论  
> 非目标：本文件不修改代码 / ADR / `casa_agent/**`

---

## 1. 概述

### 1.1 定位

Cursor API 合入 CASA 的主定位是**产物审查**，不是内容生成：

| 能力 | 定位 | 说明 |
|------|------|------|
| 审查 | **主推 / 主验收** | 独立 `agent_id`（典型 `cursor-reviewer`）在 worker 侧对产物做 JSON verdict 审查，满足 builder≠judge |
| 生成 | **可选 / 非主推** | `CursorContentGenerator` 与写路径配置存在，但不作为本轮主验收路径；默认 `auto_create_pr=False` |

报告仍由主 provider 生成；Cursor 以独立 reviewer 审查产物。审查执行落在 worker 侧**显式 review stage**（经 `AgentExecutor`），不等同于平台 ADR-48「终段 evaluator stage」字面——若需严格终段，Contract 须显式把 review 排为终段（库 ADR-0001 §2.2 / §10）。

### 1.2 本轮交付物清单

| 层 | 交付 | 路径 / 编号 |
|----|------|-------------|
| 库 extra | `casa[cursor]`（`cursor-sdk` 可选依赖） | `casa-frame/casa/extras/cursor/**`；`pyproject.toml` → `optional-dependencies.cursor` |
| 库公开面 | 懒加载导出 4 项 | `casa-frame/casa/_exports.py`：`CursorAgentExecutor` / `CursorConfig` / `CursorContentGenerator` / `CursorReviewHook` |
| 库 ADR | Cursor API 集成设计 | `casa-frame/docs/adr/0001-cursor-api-integration.md` |
| 平台 ADR | 多 provider 经济账扩展 | `casa_agent/01-decisions.md` **ADR-55**（修订） |
| 平台 ADR | 审查开关与范围 | `casa_agent/01-decisions.md` **ADR-58**（新增） |
| 测试 | 安全回归 + 功能单测 | `casa-frame/tests/extras/test_cursor.py`（step 6 后 30 passed） |

**未改内核编排**：`casa/orchestration/execute.py`、`executor.py`、既有 `PipelineHook` 基线保持不变；Cursor 能力以 optional extra + 平台装配注入，不耦合内核（对齐 ADR-8 / ADR-37）。

---

## 2. 功能变更

### 2.1 新增组件（库层）

实现落在 `casa-frame/casa/extras/cursor/`（相对 ADR 草案中的 `casa/cursor/` 命名，以本轮落地路径为准）。

| 组件 | 文件 | 职责 |
|------|------|------|
| **`CursorAgentExecutor`** | `executor.py` | **核心**。继承 `AgentExecutor`；worker 侧审查主路径；`enabled`/`review_stages` 自检；read-only 配置（local+cwd / cloud+repos）；懒加载 SDK；`api_key` executor-local；`review_timeout_seconds` + advisory semaphore |
| **`CursorReviewHook`** | `hooks.py` | **非 LLM**。`PipelineHook.on_stage_end`：stage 过滤 + Cursor provenance 校验 + verdict→audit / 可选 `fail_stage_on` 标记；禁止 import SDK / 持密钥 |
| **`CursorConfig`** | `config.py` | 配置载体：`enabled`（默认 false）、`review_stages`（默认 `[]`）、`fail_stage_on`、`cwd_policy`、`runtime`/`cwd`/`repos`、`review_timeout_seconds`（默认 120）、`per_provider_concurrency`（默认 unset）、`from_tenant_policy` |
| **`CursorContentGenerator`** | `executor.py` | 可选生成 helper；默认 `auto_create_pr=False`；非主验收 |
| **JSON 审查 schema** | `schema.py` | `REVIEW_REPORT_SCHEMA`：`{verdict, issues[], summary}`；`parse_review_text`；解析失败兜底 `verdict=conditional` + `review_parse_failed`（artifact 仅 hash/len，不写 raw_text） |
| **errors** | `errors.py` | `CursorConfigError`；`CursorReviewError`（固定码 `cursor_auth` / `cursor_timeout` / `cursor_failed`） |
| **`_runtime` 适配层** | `_runtime.py` | 唯一集中 cursor-sdk 调用点（懒加载、`build_agent_options`、`run_prompt`）；SDK 漂移只改此处 |

### 2.2 可选 extra `casa[cursor]`

- `pyproject.toml`：`cursor = ["cursor-sdk==0.1.9"]`；`all` **不含** cursor（step 3）。
- 启用路径才 `import_cursor_sdk()`；未装 extra 时启用分支抛 `CursorReviewError`（含 `pip install casa-frame[cursor]` 提示），短路路径零 SDK。
- **`import casa` 不依赖** cursor-sdk（懒加载 + `_exports` 延迟解析）。

### 2.3 公开导出（`_exports` 4 项）

```text
CursorAgentExecutor, CursorConfig, CursorContentGenerator, CursorReviewHook
```

来源：`casa/_exports.py` 映射至 `.extras.cursor`（step 3 验收）。

### 2.4 平台 ADR

| ADR | 变更类型 | 要点 |
|-----|----------|------|
| **ADR-58** | 新增 | `tenant_policy.cursor_review.{enabled,review_stages,fail_stage_on}`：mgmt 唯一写 → TenantPolicySync → worker 注入 `CursorConfig`；库不读 mgmt |
| **ADR-55** | 修订 | 经济账 Gate 扩展为**多 provider**：自托管仍按 token；Cursor 外呼按调用×单价（token=Unknown 时摊销）；未配置该 provider 计费口径前 Gate **不可验收** |

---

## 3. 流程变更

### 3.1 审查路径：从「Hook 内调 LLM」→「显式 review stage + worker executor」

**基线（既有）**：编排经 `AgentExecutor` 执行 stage（`casa/orchestration/executor.py` / `execute.py`）；`PipelineHook`（`casa/hooks.py`）提供生命周期扩展，既有门禁类 hook（如 `QualityGateHook`）在 `on_stage_end` 做非 LLM 策略，不内嵌外部 LLM 客户端。

**变更后（Cursor 审查）**：

```text
Contract/DAG 显式 review stage
        │
        ▼
worker 持 ExecutionLease（ADR-9/16）
        │
        ▼
CursorAgentExecutor.execute(agent_id=cursor-reviewer, context)
  · 自检 enabled + review_stages
  · 懒加载 SDK + 调 Cursor API（仅 worker）
  · 产出 JSON review artifact（含 provenance / inputs_fingerprint）
        │
        ▼
CursorReviewHook.on_stage_end（可选）
  · 非 LLM：过滤 stage + 校验 Cursor provenance
  · verdict → audit；fail/conditional 且 stage ∈ fail_stage_on → mark fail
```

相对「在 Hook / orch 内直接调 LLM」的反模式：Hook **降级**为排程/过滤/verdict 处置信号；LLM **只**在 worker 的 `CursorAgentExecutor` 内发生（库 ADR-0001 §2.2；实现 `hooks.py` / `executor.py`）。

### 3.2 管理侧开关与范围

对齐 ADR-58 + 库 ADR-0001 §2.4：

```text
mgmt 写 tenant_policy.cursor_review.*     （ADR-26 唯一写）
        │
        ▼
TenantPolicySync（ADR-33：redis-control → PG effective_version）
        │
        ▼
worker 读 effective policy → CursorConfig.from_tenant_policy(...)
        │
        ▼
注入 executor/hook（库不读 mgmt；orch 仅读「是否排 review stage」信号，不持凭证）
```

语义：`enabled=false` → 不排/不执行（executor skip / hook no-op）；`review_stages` 空 → 不审。

### 3.3 凭证与调用边界

| 规则 | 落地 |
|------|------|
| `CURSOR_API_KEY` 与 LLM 调用仅在 worker | ADR-37；库 ADR §9；executor 启用路径才用 key |
| 库 / orch 不持密钥、不调 SDK | Hook 零 SDK；orch 未改、不导入 cursor executor |
| `api_key` executor-local，不回写共享 `CursorConfig` | step 6 / CUR-SEC-006；`executor._api_key` |
| 审查路径禁止回退通用 `llm_config` | 仅 `provider=="cursor"` 作 dev-only；否则 `CursorReviewError("cursor_auth")`（CUR-SEC-007） |
| 序列化 / repr / 异常防泄露 | `api_key` `repr=False`；`to_dict` 掩码；MCP redact；对外固定错误码 |

### 3.4 并发模型

| 层 | 机制 | 角色 |
|----|------|------|
| **平台执行帽** | `ExecutionLeaseGate`（ADR-9/16） | **唯一**执行计数 / 准入；worker 持 lease 才调 Cursor |
| **Cursor API 保护帽** | `per_provider_concurrency` 进程内 `asyncio.Semaphore` | **仅 advisory**：等待/退避；**非准入**；默认 unset（`None`） |
| **防 hang 占槽** | `review_timeout_seconds`（默认 120） | `asyncio.wait_for`；超时 → `CursorReviewError("cursor_timeout")`（可 retry） |

禁止：用 per-provider 信号量做 lease 拒绝、或未持 lease 的全局排队（库 ADR-0001 §3.1）。

### 3.5 容错

- 失败 / 超时 / 缺 SDK / 缺 key → `CursorReviewError`（固定码），**不** fail-open 为 `verdict=pass`。
- JSON 解析失败 → `verdict=conditional` + `review_parse_failed`（非 pass）。
- 非 Cursor 异常亦包装为失败路径（step 5 正面控制已验证）。

---

## 4. 对既有 ADR 的影响

对照库 ADR-0001 §10 与平台 `01-decisions.md`（含 step 7 修订）：

| ADR | 对齐 / 修订点 |
|-----|----------------|
| **ADR-3**（30 用户 / ~60 W） | 执行帽仍由平台 lease；Cursor 保护帽默认 unset，L0 实测后再配；见本文 §6 |
| **ADR-8**（casa-frame=库） | 能力落在 optional extra，不耦合内核；未装 extra 零依赖 |
| **ADR-9 / ADR-16**（ExecutionLeaseGate 唯一执行计数） | 禁止第二执行帽 / 双计数；`per_provider_concurrency` 仅 advisory 等待，不映射 lease 拒绝 |
| **ADR-26**（mgmt 唯一写策略） | `tenant_policy.cursor_review.*` 由 mgmt 唯一写；库只提供 `from_tenant_policy` 注入点 |
| **ADR-33**（TenantPolicySync） | enabled / review_stages / fail_stage_on 经 Redis→PG effective_version 下发；worker 注入 `CursorConfig` |
| **ADR-36**（orch/worker 分解） | 审查经 worker `AgentExecutor`；句柄单次 execute 内 create+dispose；Hook 不在 orch 调 SDK |
| **ADR-37**（LLM 只在 worker） | 凭证**与调用**仅在 worker；库不内置/不持久化密钥；`api_key` 运行时注入 |
| **ADR-47**（sandbox 分级） | `cwd` 落目录策略 + 平台只读挂载；Cursor runtime **需出网**，不适用 L1「禁网」整进程模型 |
| **ADR-48**（builder≠judge） | 独立 `agent_id` + 确定性触发（enabled/review_stages/Contract）；**不等同于**终段 evaluator stage；严格终段须 Contract 显式排 |
| **ADR-55** | **修订**：L0 冒烟口径保留；经济账 Gate 扩展为多 provider（自托管 token + Cursor 调用摊销）；未配该 provider 计费口径前不可验收（链库 ADR §6/§13） |
| **ADR-58** | **新增**：Cursor 审查一键开关与 `review_stages` / `fail_stage_on` 平台路径（对齐库 ADR §2.4） |

相关但非本表逐条展开（库 ADR §10 已列）：ADR-32/35（fencing / 工具副作用 wrapper，平台装配前置）、ADR-49（model 别名路由；`composer-2.5` 仅 fallback）。

---

## 5. 安全与装配责任

### 5.1 库层 vs 平台装配（边界）

| 关注点 | 库层（`casa[cursor]`） | 平台装配（vlepontas / worker） |
|--------|------------------------|--------------------------------|
| local 审查「只读」 | containment：`Path.resolve`、拒 `..`、可选 `allowed_cwd_roots`；强制 `cwd_policy=readonly`（审查+local） | **OS 级 readonly bind mount**；无硬挂载则不得宣称审查 read-only 安全（CUR-SEC-001 / step 6） |
| `cwd` / `repos` | schema + allowlist 校验（`validate_cwd` / `validate_repos`） | 注入前按租户策略选择合法根 / 仓库集合 |
| MCP | 审查路径**默认禁止** `mcp_servers`；生成需 `allow_mcp=True` | 若启用，维护 server allowlist；禁不可信 inline command |
| `fail_stage_on` | 可按 verdict 标记 `StageResult.success=False` + 记 `inputs_fingerprint` | **须叠加**非 LLM 硬规则 / 人工批准；**不得**仅依赖库层 LLM verdict 抗 prompt 注入（CUR-SEC-005） |
| cloud 副作用 | 审查路径硬编码 `auto_create_pr=False` | cloud runtime / PR / 不可逆副作用由平台装配与 ADR-35 分类管控 |
| 凭证 | executor-local；repr/`to_dict`/异常脱敏 | worker 环境注入 `CURSOR_API_KEY`；按租户隔离；生产显式传 key |
| fencing / 幂等 | 库声明前置条件 | 经 ADR-35 wrapper：epoch fence + 幂等键 `run_id#attempt#stage` |

### 5.2 其它安全控制（已落地）

- Hook 绑定 `review_stages` + Cursor provenance（`_meta.provider=="cursor"` 或 reviewer `agent_id`）；无 provenance 不 fail（CUR-SEC-004）。
- 解析失败 artifact 不写 `raw_text`，仅 hash + len（CUR-SEC-010）。
- AGPL/许可：引入 `cursor-sdk` 前须**实现 Gate 单列确认**（库 ADR §12）；optional extra 规避强制依赖。

---

## 6. 并发与 30 用户目标

全局目标（ADR-3）：约 30 用户同时、典型 dialogue+orch+2 worker ⇒ 约 **60 worker 槽（W）**（产品目标，非 SLA）。

```text
┌─────────────────────────────────────────────┐
│ 平台 ExecutionLeaseGate（唯一准入 / 计数）   │
│  tenant active × session active × global W │
└───────────────────┬─────────────────────────┘
                    │ 持 lease 的 worker 才可执行
                    ▼
┌─────────────────────────────────────────────┐
│ CursorAgentExecutor（进程内 API 保护）       │
│  · per_provider_concurrency：advisory 等待  │
│  · review_timeout_seconds：防 hang 占槽     │
│  · rate_limit：L0 后实现（当前未启用）      │
└─────────────────────────────────────────────┘
```

分层关系：

1. **平台 lease** 决定「能不能跑 / 占不占 W」——满足 30 用户目标的主杠杆。
2. **Cursor advisory cap + timeout** 只防止外部 429 / hang 拖死已持 lease 的 worker；默认 unset 的 concurrency 跟随 lease，避免库层猜 Cursor 限流。
3. 若实测 Cursor 限流 ≪ W，应优先在**平台**做 lease/拉取节流；worker 内信号量会牺牲 lease 利用率，非首选（库 ADR §3.2）。

---

## 7. 向后兼容

| 场景 | 行为 |
|------|------|
| 未安装 `casa[cursor]` / 未装 `cursor-sdk` | `import casa` 成功；启用路径才报错；内核无 Cursor 依赖 |
| `CursorConfig.enabled=false`（默认）或未注入 | executor 返回 skip artifact；hook no-op；零 SDK |
| `review_stages` 为空（默认） | 不审、不排/不执行 Cursor review |
| 既有 stage / `AgentExecutor` / `PipelineHook` | 未改 `orchestration/` 基线；既有 executor（`Simple`/`Mock`/`Sandboxed`/`Code`）不受影响 |
| 未 register `CursorReviewHook` | 无 Cursor verdict 处置副作用 |

结论：**默认关闭 + optional extra** ⇒ 对未采用 Cursor 审查的部署，框架行为与合入前一致。

---

## 8. 遗留与后续

| 项 | 状态 | 说明 |
|----|------|------|
| **`rate_limit`（令牌桶）** | L0 后实现 | 已从 `to_dict` 移除，避免假承诺（CUR-SEC-009 / B-3）；ADR 仍保留「实测后再配」口径 |
| **cloud runtime 副作用** | 平台装配 | 隔离 VM、PR 策略、不可逆副作用的 retry/对账属平台 + ADR-35；库审查路径默认关 PR |
| **AGPL / 许可** | 实现 Gate 确认 | 合入/发布 optional extra 前须单列确认 `cursor-sdk` 许可风险（库 ADR §12） |
| **ADR-55 经济账验收** | 口径已修订，验收待配置 | 平台须为 Cursor provider 配置计费口径后，Gate 方可验收 |
| **L0 real-provider 冒烟** | 待跑 | 5–10 并发：限流 / TTFT / 调用数×duration；token=Unknown |
| **`Agent.resume` 跨进程** | P2+ | 句柄不跨 stage；跨进程续接推迟 |
| **严格 ADR-48 终段** | Contract 显式排 | 本集成提供独立 reviewer，不自动等同终段 evaluator |

---

## 附录 · 关键引用

| 类型 | 路径 |
|------|------|
| 库 ADR | `casa-frame/docs/adr/0001-cursor-api-integration.md` |
| 平台 ADR | `casa_agent/01-decisions.md`（ADR-55、ADR-58） |
| 实现 | `casa-frame/casa/extras/cursor/{config,executor,hooks,schema,errors,_runtime}.py` |
| 导出 | `casa-frame/casa/_exports.py` |
| 编排基线 | `casa-frame/casa/orchestration/{execute,executor}.py`；`casa-frame/casa/hooks.py` |
| 实现摘要 | `.cursor/artifacts/20260720-cursor-impl/step-3/result.md` |
| 安全吸收 | `.cursor/artifacts/20260720-cursor-impl/step-6/result.md` |
| Review 输入 | `.cursor/artifacts/20260720-cursor-impl/step-4/review.md`、`step-5/review.md` |
