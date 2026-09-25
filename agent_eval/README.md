# Agent Lite Eval Kit

面向终端 agent 的可迁移评测题包 v2：**30 个可执行任务入口、3 档规模、确定性数据生成、客观评分、阶段快照和上下文压力测量**。新增 24 个入口：8 道历史资料题、10 道状态流程题、6 道分阶段实现题。

针对“最长任务只到 16% 上下文”的分析、新题目册与运行命令，优先阅读 **[长任务 v2 指南](docs/LONG_TASKS.md)**。历史资料题长档默认 40 阶段，每阶段约 32,000 字符，可配置至 128 阶段。字符规模不等于实测 token；功能成功与压力达标分别报告。题目入口共享部分生成和业务机制，不等于 30 个统计独立能力。

这不是 agent-lite 的既有成绩，也不是官方 benchmark。自测只验证题包与评分器。实际准确率、耗时和成本必须运行被测 agent 后填写。

## 从哪里开始

1. 阅读 [原六题题目册](docs/TASKS.md) 和 [新增 24 题](docs/LONG_TASKS.md)，了解输入、难点、交付和验收。
2. 按下面的命令跑一题，确认 agent 接收材料、落盘、评分链路正常。
3. 按 [实验方案](docs/PROTOCOL.md) 固定模型、预算、种子和对照组，再批量测。
4. 用 [报告模板](templates/REPORT.md) 留证据，用其中的简历模板填入实测值。
5. 需要外部可比性时，参考 [公开题源调研](docs/SOURCES.md) 接入正式基准。

## 题目

| ID | 题目 | 检验重点 | 自动评分 |
| --- | --- | --- | --- |
| A01 | 跨分片交易对账 | 全量处理、版本去重、整数金额、零余额 | 精确核对余额与生效事件列表 |
| A02 | 多环境配置迁移 | 定点修改、版本优先级、单位取整、保护无关文件 | 全配置语义、修改清单、只读文件哈希 |
| A03 | 长日志证据核查 | 超长输出检索、权威证据、未知不猜、准确行号 | 每条结论、数字与证据位置 |
| C01 | 事件账本修复 | 顺序、幂等、资金不足、边界与回归 | 未公开输入上的程序行为 |
| C02 | 持久化任务队列 | SQLite、重启、重复投递、过期租约、旧确认 | 多进程连续操作及结果 |
| L01 | 分阶段库存结算 | 旧规则保留、新规则更新、累计去重、阶段完整性 | 每阶段不可回补的快照 |

上表为原六题。新增 M01–M08（多时点、撤销、权威、约束、引用、双时态、例外的历史资料）、W01–W10（预留、事务、部署、权限、迁移、图、副本、配额、构建、SLA）、R01–R06（前六种状态机的逐阶段实现）；运行 `python eval.py list` 查看全部题目。

`smoke / standard / long` 是规模档，不是独立能力。随机种子也不等于新题族。旧 L01 标准/长档为 8/16 阶段；新增 M/W/R 为 4/16/40 阶段，可用 `--stages` 调整。C01/C02 的 long 增加的是隐藏验收规模，不会增加 agent 作答时上下文。做得快不扣分。

## 环境与迁移

复制整个 `agent_eval/` 到任意目录即可。生成器、评分器和离线自测只依赖 **Python 3.10+ 标准库**，不需要 agent-lite、模型密钥、网络或第三方库；不修改原项目运行时代码。可选自动适配器 `run_agent_lite.py` 才需要指定 agent-lite 源码路径、其依赖和模型配置。

下面命令都在复制后的 `agent_eval/` 内执行，在 PowerShell、bash 中均可使用：

```text
python eval.py list
python selftest.py
python eval.py prepare --task A01 --seed 17 --scale smoke --out runs/a01-17-r1
```

生成的结构：

```text
runs/a01-17-r1/
  workspace/            只把这部分交给被测 agent
    PROMPT.md           完整题面
    input/              数据（不同题可能还有 configs/、spec/、src/）
  private/              评测者持有：标准答案、隐藏行为案例、未来阶段、快照
  run.json              评测者记录：版本、初始题目哈希、分组、时限、状态
  results/grade.json    评分后生成，含逐项判定
```

**把题包转交给别的评测者时复制整个文件夹；让另一个 agent 作答时只交 workspace。** `private` 是目录组织，不是权限隔离。正式盲测应在独立 VM/容器中运行 agent，只映射 workspace，隐藏题包源码、评分器和 private。agent-lite 的 `--workspace`、文件夹命名和单独的 bash 沙箱都不能单独保证所有工具无法读取其他路径；无法真正隔离时在报告标注“非盲测”。

## 完整跑一题

先准备上面的 A01，再启动计时。下面模型名 `MODEL_ID`、版本名 `agent-lite@COMMIT` 必须替换为实际值：

```text
python eval.py start --run runs/a01-17-r1 --agent agent-lite@COMMIT --model MODEL_ID --variant baseline --repeat 1 --budget-seconds 1800
```

在 agent 中设置工作目录为该 run 的 workspace，发送：

> 阅读当前目录的 PROMPT.md，完成其中任务并落盘所有要求的交付物。

agent-lite 示例（使用绝对路径，正式测评把实际后端记录下来）：

```text
agent-lite --ui cli --new --workspace ABSOLUTE_WORKSPACE --model MODEL_ID --sandbox wsl --permission-policy auto
```

以上命令使用现有模型配置，会在你实际运行时产生模型调用。`eval.py` 不调用模型；可选 `run_agent_lite.py` 在你主动执行后自动调用模型。本次交付未运行付费评测。`auto` 仅适用于已准备好的专用评测环境。其他 agent 可使用同一题面和评分器。

**M 系列例外：**不要仅发送“阅读 PROMPT.md”。必须将评测者目录下 `next_prompt.txt` 的内容原样作为本轮用户消息发送；它包含当前阶段的新资料，且不得作为整套历史文件放进 workspace。后续阶段同样发送文本。自动适配器已处理此差异。

agent 结束后停止其写入，评测者复制 `templates/telemetry.json` 到 run 目录并填写。人工只递交事先确定的题面、发布阶段、按协议重启不算解题帮助；指出错误、提供修复方向、代写答案算帮助。`human_interventions` 不填或非 0 时，评分器不会判“自主成功”。

```text
python eval.py finish --run runs/a01-17-r1 --status completed --telemetry runs/a01-17-r1/telemetry.json
python eval.py grade --run runs/a01-17-r1
python eval.py summarize --root runs --out runs/summary.json
```

如果超时/报错，分别使用 `--status timeout` / `agent_error` / `infra_error`，仍然评分并保留样本。不要只留成功案例。`start/finish` 记录墙钟且完成超预算会归为 timeout；**控制器不启动或终止 agent，不强制 token/工具预算**。达到预算由评测者或外部执行器停止 agent。计时包含运行期间人工等待，统一操作流程以避免组间偏差。

代码题的评分需要执行提交：在独立、无凭据、无网络的评测 VM/容器内运行：

```text
python eval.py grade --run runs/c01-17-r1 --execute-submission
```

代码评分器为每个案例复制 src/，对每次提交进程设 5 秒超时；C02 在一个案例内用同一数据库启动多个进程。临时目录和 Python `-I` **不是安全沙箱**；宿主机模式没有完整进程树/内存/磁盘/网络隔离。正式测评由外围环境提供隔离与资源上限。超过限制或无合法 JSON 即不通过。

## 长任务、压缩和恢复

生成 L01 后按正常流程 start。agent 完成阶段 1 时，评测者执行：

```text
python eval.py checkpoint --run runs/l01-17-r1
```

此命令冻结本阶段产物、发布下一批输入，并打印下一条用户消息（也保存在 `next_prompt.txt`）。把打印的原文发给**同一个会话**。阶段推进不返回对错，错误答案也会被冻结。最后一个阶段仍需执行一次 checkpoint，再 finish/grade。只提交最终余额或最后统一补交快照不能通过。

重启实验用 `start --protocol resume`：标准档在完成并冻结阶段 4 后，退出 agent 进程，保留会话与 workspace；用原 session ID 恢复，再发送阶段 5 的消息。评测者记录：

```text
python eval.py restart --run runs/l01-17-r1 --note "阶段4后正常退出；以原SESSION_ID恢复；日志见session-restart.txt"
```

这个命令只是证据记录，不会代替实际重启。默认不做随机强杀或网络注入。压缩实验用 `start --protocol compaction`，记录实际成功的压缩事件；没有压缩发生，不能算压缩后成功。更完整的规则、对照组和有效样本条件见 [实验方案](docs/PROTOCOL.md)。

## 评分含义

- `artifact_pass`：所有必需交付/行为通过、保护文件未变、全部阶段冻结。
- `autonomous_success`：还要求在预算内完成、人工解题帮助为 0、所选协议有压缩/重启记录。
- `partial_score`：artifact/behavior 检查通过比例，仅排错用，不是主指标，不能跨题族直接比较难度。
- `pressure_qualified_success`：在功能成功基础上达到事先声明的真实峰值/压缩次数门槛；无事件证据时不能判达标。需要压缩的任务还须有压缩后的已评分阶段。
- `checks`：字段、记录、快照、行为、完整性明细。额外字段、数字类型错误、数组乱序也可能失败。

正确答案不允许抵销保护文件变更。最终哈希只能检测留下的修改，无法证明“未修改后又还原”或“从未越界读取”；过程合规需要外部工具审计。未知 token/价格记 `null`，不能当 0 成本。

## 文件索引

- `tasks.py`：题面、确定性数据、参考计算和隐藏案例。
- `long_tasks.py`：新增 24 题的规范、历史资料和状态机生成。
- `eval.py`：生成、计时登记、阶段冻结、评分、汇总。
- `pressure.py`：从事件区分峰值/末态占用，检查压力资格；可单独检查 session 末态。
- `run_agent_lite.py`：可选实际模型调用适配器；同会话自动投题、冻结和 usage 记录。
- `selftest.py`：参考解正例、错误答案反例、独立手算边界与协议自测。
- [TASKS.md](docs/TASKS.md)：六题详情与四道后续扩展题设计。
- [PROTOCOL.md](docs/PROTOCOL.md)：样本、预算、公平对照、指标和统计边界。
- [LONG_TASKS.md](docs/LONG_TASKS.md)：16% 问题分析、24 道新题、压力实验命令。
- [SOURCES.md](docs/SOURCES.md)：公开题源、原版入口与许可注意。
- [REPORT.md](templates/REPORT.md)、[telemetry.json](templates/telemetry.json)、[experiment.json](templates/experiment.json)：记录模板。

v2 范围：历史资料、本地数据/配置、Python 行为、多阶段交付和测量；没有浏览器视觉、联网搜索、并发竞争、真正的分布式事务评分。旧 X01–X04 仍是未实现设计稿，未计入 30 个可运行入口。题包自测通过不意味着测得 agent 的任何成功率。
