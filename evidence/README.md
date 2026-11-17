# 实际验证记录

验证日期：2026-11-17，Asia/Shanghai。初次演示使用考试 API 的真实模型 `linext-5.3-luna`；架构改造后的补充验证使用 `deepseek-v4-pro`。没有用假响应替代真实执行。

## 架构分层与 DeepSeek 续聊：已通过

- 架构改造提交 `cd9e2ff` 的 [Docker 构建、启动和重启持久化验证](https://github.com/ZYJda/cloud-agent-workbench/actions/runs/44037052817) 全部通过。
- 本机离线回归增加至 17 项，全部通过；覆盖新增上下文中间件、每轮成果哈希、Gateway/Harness 依赖边界及每会话 DeepSeek 能力配置。
- 原考试临时凭据过期返回 401 后，切换至 DeepSeek 官方 `https://api.deepseek.com`，模型 `deepseek-v4-pro`，继续使用 Codex CLI 0.169.0，无中转聊天模拟。
- 在 Windows 本机实际运行 `python demo/check_harness.py`：上传包含 6、7 的文本，第一轮读取并计算得到 13，写入 artifacts/result.txt；第二轮沿用同一个 CLI thread ID，保留结果并追加 VERIFIED。两轮均 succeeded。
- 已验证两轮成果清单的 SHA-256 不同，最终下载内容与第二轮登记的 SHA-256 一致。会话、任务状态与成果记录见 [harness-refactor.json](harness-refactor.json)，29 条原始执行事件见 [harness-refactor-events.json](harness-refactor-events.json)。
- 此项 DeepSeek 真实模型验证运行于本机，不表述为容器内 DeepSeek 验证。下方既有容器四轮演示使用原考试模型；新的 Docker 验证不调用模型。新凭据仅保存在本机被忽略的 .env，未写入公开仓库。

## Windows 本机真实流程

`live-demo/summary.json` 记录会话 ID，两个会话 JSON 记录每轮任务和持久化的 CLI thread ID；`live-demo/events.json` 保存 342 条真实执行事件（已通过应用的密钥脱敏逻辑）。

- 代码会话首轮：生成 CSV 分类金额汇总工具、测试、示例数据及 README；运行 25 个测试全部通过。
- 同一代码会话续聊：增加 `--json`；测试增加到 34 个并全部通过。独立复核再次运行生成的测试文件，结果仍为 34/34 通过。
- 材料会话：通过上传接口提交 demo/sales.csv 和 demo/brief.txt，生成报告与核算脚本；两个月收入分别为 70000、78000，毛利为 30600、35700，与人工复算一致。
- 同一材料会话续聊：保留报告原文，追加管理层摘要和三项下周行动。报告区分事实、推测与信息局限。
- 两会话同时提交并执行，随后分别通过原 thread ID 续聊。两个工作区 ZIP 下载均返回 200，实际下载见 code.zip 和 report.zip。
- 浏览器实际验证：切换会话、刷新后回放历史、预览 README、显示文件列表和完成状态。截图见 screenshots/。
- 已实际停止一个重复诊断任务，状态为 cancelled；测试覆盖排队取消、继续提交、异常退出、服务重启后的 interrupted 状态。

这些文件保留生成时的真实内容，可能包含 Windows 路径或助手的调试过程。演示报告和工具只是用于验证工作台，不属于工作台自身源码。

## Docker 与回归

- [Docker 构建与启动验证](https://github.com/ZYJda/cloud-agent-workbench/actions/runs/44031903409)：真实 Linux runner 构建镜像、启动容器、访问网页和健康接口、执行 `codex --version`、重启后重新读取 SQLite 中的会话，全部通过。
- 后续回归共 13 项：`python -m unittest discover -s tests -v`，全部通过。测试中的 CLI mock 仅用于离线故障场景，不作为真实模型证据。
- 本机 Docker Desktop 已安装，但 Windows 未启用 Virtual Machine Platform，无法启动 Linux engine；没有将本机原生 Python 运行冒充为 Docker 运行。容器验证在公开 GitHub Actions 中执行。
- 首轮容器真实模型演示遇到 429 限流，失败记录保留在 Actions。后续调整为 `MAX_CONCURRENT_RUNS=1`，避免与本机运行同时占用 API 并发。

## Docker 内真实模型演示：已通过

[完整 Actions 运行（44033399517）](https://github.com/ZYJda/cloud-agent-workbench/actions/runs/44033399517) 于 2026-11-17 15:00 左右完成，job 总耗时 16 分 19 秒，结论 success。

该运行在 Linux Docker 容器中使用 Codex CLI 0.169.0 和加密服务端凭据，创建两个工作会话并通过队列依次执行，完成代码开发、文件上传分析以及各自的一次后续修改，四轮均 succeeded。并发限制设为 1，不将队列执行表述为两个 Agent 同时运行。

`docker-demo/` 保存该运行下载的原始验证产物：会话 JSON、CLI 事件、容器日志、工作流日志及两个成果 ZIP。已核对代码 ZIP 包含 summary.py、test_summary.py、README.md、sample.csv；报告 ZIP 包含 report.md、核验脚本与上传材料。源码 ZIP 与密钥扫描均不包含 .env 或运行数据卷。

## 重现

1. 按 README 配置并启动 Docker。
2. 运行 `python demo/run_demo.py`，会消耗真实模型额度。
3. 在浏览器查看两条新会话的任务、文件及续聊结果。
4. 也可通过 Actions 的 `Real Agent Docker demo` 手动运行；需要仓库加密 Secrets：AGENT_API_KEY、AGENT_BASE_URL、AGENT_MODEL。
