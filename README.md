# AI 原生软件工程 Agent Team v0.2

Python 3.11+、Pydantic 2、SQLite。一个代码 Orchestrator 调度 Planner / Builder / Verifier / Operator 四个专业角色。默认 MockLLM，无需 API 密钥，可离线执行固定 calculator 示例。OpenAI Agents SDK 是可选依赖；未配置密钥时不会调用外部模型。

v0.2 在原有审批流上增加四件事：导入本地 Git 仓库并在独立 worktree 中作业、把实现审批绑定到补丁和基线、由 Verifier 独占验收测试、以及可选的容器测试执行器。

## 快速运行

在本项目目录执行。PowerShell 里不要把下面的注释和命令一次性粘贴，注释行不会激活虚拟环境。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m agent_team doctor
python -m unittest discover -s tests -v
python -m examples.dry_run --home .team-demo
```

若环境已有 Pydantic 2，可直接运行后三条命令。`examples/dry_run.py` 只对内置 calculator fixture 使用明确标记的模拟审批，不会导入外部仓库，也不会批准真实任务。

Dry-run 不调用外部模型、不做外部部署：会写入示例代码、在宿主机运行 unittest、生成本地 ZIP。MockLLM 只实现加法示例，不会根据任意需求生成软件。

## CLI

```sh
python -m agent_team create my-project --requirement "实现支持正负数的 add(a,b)" --allow-test-execution
python -m agent_team create fix-add --requirement "修复 add" --repo D:\work\sample --allow-test-execution
python -m agent_team create fix-add --requirement "修复 add" --repo D:\work\sample --dirty committed
python -m agent_team create fix-add --requirement "修复 add" --repo D:\work\sample --dirty include
python -m agent_team create fix-add --requirement "修复 add" --repo D:\work\sample --executor container
python -m agent_team list
python -m agent_team status RUN_ID
python -m agent_team next RUN_ID
python -m agent_team artifacts RUN_ID
python -m agent_team approve RUN_ID APPROVAL_ID --actor your-name --note "已审阅补丁"
python -m agent_team next RUN_ID
```

用输出中的真实 ID 替换占位符。每次 `next` 只推进一步，审批通过后还需 `next` 恢复流程。拒绝用 `approve ... --reject --note "原因"`，run 进入 BLOCKED。达到三轮实现重试也会阻塞。

`--home PATH` 是全局选项，放在子命令之前，默认 `.team`。导入仓库时，`--home` 必须在该仓库目录之外。

## 导入本地仓库

`--repo` 指向一个已有提交的本地 Git 工作区。Orchestrator 用 `git worktree add -b agent-team/<run_id>` 在 run 目录建立任务分支和工作区，不在原始工作区执行 checkout、stash、reset、commit 或 push。

有未提交修改时必须显式选择，否则直接拒绝并且不创建 worktree：

- `--dirty committed`：任务以当前 HEAD 为基线。未提交内容留在原始工作区，不复制。
- `--dirty include`：把相对 HEAD 的差异和未跟踪的普通文件复制到任务 worktree，并只在任务分支上提交这份快照。未跟踪的隐藏文件或符号链接会拒绝导入，避免把密钥或链接带进任务分支。原始工作区文件保持不动。

仓库的 Git 目录会登记这个 worktree 和任务分支。这不改变原始工作区里的已跟踪文件、当前分支或 HEAD。任务分支上的提交使用命令行临时身份 `Agent Team`，不写全局 Git 配置。

## 补丁审批

Builder 仍提交完整文件内容。Orchestrator 对照当前基线生成统一 diff，写入 `Implementation.patch`。审批 subject 是补丁文本加上基线的 SHA-256。基线包含任务分支 HEAD（没有仓库时为空）和工作区文件摘要。

文件或 HEAD 变化后，subject 对不上，原来的 high_risk_change 审批失效，必须重新审批。通过后才写入任务工作区；导入的仓库还会在任务分支上再做一个提交。`artifacts` 里的 `pending_proposal` 和实现产物都能看到补丁与基线。系统以文件内容派生出的 diff 为准，不采用模型另写的一份 diff。

## 开发测试与验收测试

- `tests/`：开发测试，Builder 可以编写和修改。
- `acceptance/test*.py`：验收测试，只允许 Verifier 在首次进入 TEST 时生成并锁定。

Builder 不能写入或运行 `acceptance/`。Verifier 不能写验收目录以外的文件。交付前两套测试都必须通过。验收文件摘要变化会把 run 标为 BLOCKED，不会交回 Builder 去改验收标准。验收套件本身有误时，需要新建 run，而不是让实现者改测试。

## 容器测试执行器

默认仍是宿主机执行，并且必须带 `--allow-test-execution`。宿主机执行不是沙箱。

`--executor container` 才启用容器。运行参数固定为：`--network none`、丢弃全部 capabilities、只读根文件系统、以非 root 用户运行、内存 `512m`、CPU `1`、进程数 `128`、超时 30 秒。容器环境不继承宿主机密钥，也不挂载 Docker socket。

未找到 `docker` 或 `podman`、守护进程没有运行，或本地没有镜像 `python:3.11-slim`（可用 `AGENT_TEAM_TEST_IMAGE` 指定已有镜像）时，命令会明确失败。为了保持断网，不会自动拉取镜像，也不会改回宿主机执行。

## 状态与职责

```text
REQUIREMENT → APPROVAL(requirement) → CONTEXT → ARCHITECTURE
 → APPROVAL(architecture) → IMPLEMENTATION
 → APPROVAL(high_risk_change = patch + baseline) → 写入并在任务分支提交
 → TEST（锁定验收测试，开发测试和验收测试都要通过） → REVIEW
 → DELIVERY → [APPROVAL(production)] → OPS → DONE
TEST FAIL / REVIEW BLOCK → IMPLEMENTATION（最多 3 次）
验收测试被改写，或任何审批拒绝 → BLOCKED
```

八阶段路由仍在 `agent_team/llm.py` 的 `ROLES`。四个角色不变。真实 SDK 按调用创建相应角色 Agent，没有自治循环，也不能互相授权。

## 审批和安全边界

- 需求、架构必须逐项审批。所有代码变更都进入 high_risk_change，并绑定本次补丁和基线。
- 生产审批绑定已测试、已审查的源码摘要。`staging` 和 `production` 都只生成本地 ZIP，没有云端部署、在线监控或回滚。
- Planner、Verifier、Operator 没有产品源码写权限。Verifier 只能写验收测试。Git 命令不允许模型随意调用；仓库导入由 Orchestrator 执行固定参数。
- 路径限制在 workspace 内，拒绝绝对路径、上级路径、隐藏/控制目录和符号链接逃逸。不支持删除。上下文限制 500 KB，单文件 100 KB。
- 宿主机 shell 使用固定参数列表和 `shell=False`，超时 30 秒，环境变量只保留运行所需的基础项。
- 这些应用层控制不是操作系统沙箱。容器模式只是额外的隔离选项，也不能替代不可信代码所需的独立环境。
- 审批 actor 是本地审计标签，没有登录认证或签名。不要把本系统当作多租户或强隔离安全系统。

## 持久化与目录

```text
agent_team/
  schemas.py       # 工程状态、补丁和验收契约
  store.py         # SQLite 与单写者事务
  tools.py         # 文件、补丁、测试入口
  repo.py          # 只读检查原始仓库，写入仅发生在任务 worktree
  container.py     # 容器测试执行器
  llm.py           # MockLLM / OpenAIAdapter / 四个角色
  orchestrator.py  # 状态转换、审批、回退、打包
  cli.py           # 本地命令行
examples/dry_run.py
tests/
```

每个 run 的 workspace 要么是空目录，要么是 linked worktree。SQLite 是状态权威记录。事务不能原子回滚已经写入的文件或已经在任务分支上创建的提交；崩溃后需要人工查看 workspace，再决定是否重试。状态不会把未通过的测试报成通过。

## OpenAI 接入

```sh
python -m pip install -e ".[openai]"
```

PowerShell 里只在当前进程设置密钥，不要写进代码或仓库：

```powershell
$env:OPENAI_API_KEY="你的密钥"
$env:OPENAI_MODEL="账户可用的模型 ID"
python -m agent_team create real-project --adapter openai --requirement "你的需求" --allow-test-execution
```

程序不自动读取 `.env`。缺少 SDK、模型或密钥会直接报错，不会静默退回 Mock。API 会收到需求、阶段产物和 workspace 文本。默认关闭 SDK tracing。未固定模型 ID。

## 已验证范围与限制

测试覆盖原有八阶段流程，以及：

- 脏仓库在未选择策略时被拒绝，原始文件摘要不变。
- `--dirty committed` 不复制未提交文件；`--dirty include` 只把快照提交到任务分支。
- 一个真实的临时 Git 仓库中，错误的 `add` 会在任务分支修好，原始分支和文件保持导入前的状态。开发测试和验收测试都通过后才打包。
- 补丁审批在基线文件变化后失效。
- Builder 不能改验收测试；错误的验收测试会在三次实现后 BLOCKED，并且不会产生交付包。
- 容器命令默认断网、限制资源，并且不携带宿主机密钥。运行时缺失时不会回退到宿主机。

尚未验证或故意不做的部分：外部模型的真实调用、镜像自动拉取、云部署、多用户签名审批、子模块、重命名/删除、把隐藏文件导入 worktree，以及把容器资源限制当成完整的内核沙箱。共享仓库里的 Git hooks 仍可能在任务分支提交时执行。以点开头的文件不进入基线摘要，Builder 也不能修改它们；已跟踪的这类文件仍会在 `--dirty include` 时复制到任务分支。
