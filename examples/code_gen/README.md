# 代码生成场景示例

## Git 钩子（GitHook）

`git_hook.py` 演示如何在代码 stage 边界自动创建分支并提交。

```bash
# 在 Orchestrator 中注册
from examples.code_gen.git_hook import GitHook
from casa import HookRegistry

hooks = HookRegistry()
hooks.register(GitHook(repo_path="/path/to/your/repo"))
```

配合 `CodeAgentExecutor` 使用：执行后 `_meta.changed_files` 会列出 git 变更文件。

## 相关 API

- `ArtifactStore.write_text()` / `read_text()` — plan 内纯文本产物
- `CodeAgentExecutor` — 沙箱挂载仓库并收集变更
- `RawFilesRenderer` — 终态 zip 打包（`renderer="raw_files"`）
- `StageRunner(schema_validator=lint_fn)` — 注入 lint/test 校验函数
