# AI 原生软件工程 Agent Team MVP

Python 3.11+、Pydantic 2、SQLite。一个代码 Orchestrator 调度 Planner / Builder / Verifier / Operator 四个专业角色。默认 MockLLM，无需 API 密钥，可离线执行固定 calculator 示例。OpenAI Agents SDK 是可选依赖；本次环境未安装，真实 API 调用尚未验证。

## 快速运行

在本项目目录执行：

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -e .
python -m agent_team doctor
python -m unittest discover -s tests -v
python -m examples.dry_run --home .team-demo
```

若环境已有 Pydantic 2，可直接运行后三条命令，不需安装 SDK 或联网。打包内的 `evidence/demo-run.json` 是完整演示输出，`evidence/demo-data/` 包含实际 SQLite、八类产物、生成源码、测试结果与 staging ZIP。

Dry-run 仅指不调用外部模型、不做外部部署：会真实写入示例代码、运行 unittest、生成本地 ZIP。只有此固定 fixture 脚本使用明确标记的模拟人工审批，CLI 不自动审批。MockLLM 仅实现加法示例，不会根据任意需求生成软件。

## CLI 使用

```sh
python -m agent_team create my-project --requirement "实现支持正负数的 add(a,b)" --allow-test-execution
python -m agent_team list
python -m agent_team status RUN_ID
python -m agent_team next RUN_ID
python -m agent_team artifacts RUN_ID
python -m agent_team approve RUN_ID APPROVAL_ID --actor your-name --note "已审阅产物"
python -m agent_team next RUN_ID
```

用输出中的真实 ID 替换占位符。每次 `next` 只推进一步，审批通过后还需 `next` 恢复流程。`status` 显示当前阶段和审批记录；`artifacts` 显示最新结构化产物和待审批代码全文。拒绝用 `approve ... --reject --note "原因"`，run 进入 BLOCKED；修改需求后新建 run。达到三轮实现重试也会阻塞，避免无限循环。

`--home PATH` 是全局选项，放在子命令之前；默认 `.team`。每个 run 有独立 workspace。要导入小型现有项目，在 CONTEXT 阶段前复制所需文本源码到对应 workspace；不要放密钥、依赖目录、仓库控制文件。

不带 `--allow-test-execution` 创建的 run 默认禁止执行代码，在 TEST 阶段安全报错。确认信任代码后，可新建带该选项的 run。实际不可信生成代码应在独立低权限容器中运行整个项目。

## 状态与职责

```text
REQUIREMENT → APPROVAL(requirement) → CONTEXT → ARCHITECTURE
 → APPROVAL(architecture) → IMPLEMENTATION
 → APPROVAL(high_risk_change) → IMPLEMENTATION → TEST → REVIEW
 → DELIVERY → [APPROVAL(production)] → OPS → DONE
TEST FAIL / REVIEW BLOCK → IMPLEMENTATION（最多 3 次）
任何审批拒绝 → BLOCKED
```

Planner 生成 Requirement / Architecture，Context 由确定性工具收集。Builder 返回 Implementation 文件变更提案，由受控工具应用。Verifier 的 TEST 由真实 runner 执行，REVIEW 通过结构化模型审阅。Operator 的 DELIVERY 由本地打包工具执行，OPS 生成后续建议。所有交接通过 Pydantic schema 和持久化 JSON，不使用自由聊天传递状态。

八阶段的路由位于 `agent_team/llm.py:ROLES`，`LLMAdapter.generate(role, stage, payload, schema)` 是拆分八个专家的接口；各阶段独立 schema 与 artifact 编号不需要改变。真实 SDK 按调用创建相应角色 Agent，没有自治循环或 agent 互相授权。

## 审批和安全边界

- 需求、架构必须逐项审批。实现阶段采取保守策略：**所有代码变更**都进入 high_risk_change gate，覆盖无法从文件名可靠判断的语义风险。
- 代码审批绑定提案和原始 workspace 的 SHA-256；源文件变动会重新要求审批。生产审批绑定已测试、已审查的源码摘要；交付前再次检查摘要。
- Planner、Verifier、Operator 没有源码写权限。Builder 可写源码、运行允许的测试；Verifier 委托确定性 TestRunner。Git 仅允许 status 和禁用外部 diff/textconv 的 diff，禁止 commit/push/reset。
- 路径限制在 workspace 内，拒绝绝对路径、上级路径、隐藏/控制目录、符号链接逃逸；不支持删除。上下文限制 500 KB，单文件 100 KB。
- shell 使用固定参数列表和 `shell=False`，不接受任意模型命令；执行 30 秒超时，环境变量仅保留基础运行变量，避免将 API key 传给测试进程。
- **这些是应用层控制，不是 OS 沙箱。** Python 测试可以访问当前用户可访问的文件、网络、启动子进程或伪造输出。超时不保证清理所有后代进程，输出捕获也不是硬内存上限。只在可信 fixture 或隔离容器执行；对抗恶意测试、资源限制和不可伪造验收需下一阶段独立 runner。
- 审批 actor 是本地审计标签，没有登录认证或签名。能修改数据库/程序的人能改审批；不要作为多租户或强隔离安全系统使用。
- production 默认不可进入；创建 run 时指定 `--target production` 后仍须独立审批。**staging 和 production 都仅生成本地 ZIP**，没有云端部署、在线监控或回滚服务。接入真实部署应替换 DELIVERY 工具并保持审批及摘要校验。

## 持久化与目录

```text
agent_team/
  schemas.py       # 工程状态及阶段契约
  store.py         # SQLite 与单写者事务
  tools.py         # 文件、git、测试工具
  llm.py           # MockLLM / OpenAIAdapter / 专家路由
  orchestrator.py  # 状态转换、审批、回退、打包
  cli.py           # 本地命令行
examples/dry_run.py
 tests/test_workflow.py
.team/
  team.sqlite3     # runs / state / artifacts / approvals / events
  runs/<run_id>/
    workspace/
    artifacts/
      01_requirement/
      02_context/
      03_architecture/
      04_implementation/
      05_test/
      06_review/
      07_delivery/
      08_ops/
    staging-<source_digest>.zip
```

SQLite JSON 是状态与产物的权威记录，目录 JSON 是可读副本。阶段执行持有 SQLite 写锁，支持串行继续；其他写入超时应稍后重试。本 MVP 不适用于高并发。数据库事务无法原子回滚文件写入或模型调用：中途崩溃可能留下未引用的文件产物或部分写入；状态不会虚报通过，基线摘要变化会重新审批，但需人工检查 workspace 后重试。生产版应增加隔离 checkout、原子补丁、作业队列和恢复日志。

## OpenAI 接入

```sh
python -m pip install -e ".[openai]"
# PowerShell:
# $env:OPENAI_API_KEY="你的密钥"
# $env:OPENAI_MODEL="账户可用的模型 ID"
# POSIX: export OPENAI_API_KEY=... ; export OPENAI_MODEL=...
python -m agent_team create real-project --adapter openai --requirement "你的需求" --allow-test-execution
```

`.env.example` 仅作模板，程序不自动读取 .env；请设置进程环境变量。缺少 SDK、模型或密钥会明确报错，不会静默退回 Mock。API 会收到需求、阶段产物和 workspace 文本，请自行确认这些资料适合发送。默认关闭 SDK tracing；模型调用可能产生费用。未固定模型 ID，由用户选择账户可用模型。

实现依据官方 [Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) 和 [Agent definitions](https://developers.openai.com/api/docs/guides/agents/define-agents) 的 `Agent(output_type=...)`、`Runner.run_sync` 接口。SDK 依赖范围用于 MVP，生产使用前需锁定版本并进行真实 API 集成测试。

## 已验证范围

基础测试覆盖完整八阶段流程、持久化重载、审批不可跳过/拒绝、production gate、TEST FAIL / REVIEW BLOCK 回退、重试上限、权限与路径、无测试失败、测试执行 opt-in、源码变动使审批/测试失效。`doctor` 为启动检查。外部模型、云部署、强沙箱、多用户审批、真实线上指标尚未实现或验证。
