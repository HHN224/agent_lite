# 从 150 行脚本到 Agent Runtime：复刻 pi-agent 的第一课

> 日期：2026-08-20
> 项目：agent lite —— 用 Python 复刻自己的 pi agent 类物
> 阶段：核心 runtime 从无到有（9 个 commit，6 个阶段）
> 方向：自底向上分层（ai → agent_core → coding_agent），底层不能调用上层

## 一、背景

项目最初只是一个约 150 行的教学型 Agent：`execute() -> str`，一切工具返回都是字符串，AgentLoop 里塞满了 `print()`，循环逻辑和 UI 焊死在一起。今天的目标很朴素——让它从"脚本"进化成"runtime"：可复用、可单步、可中止、可被外部查询状态。

## 二、完成了什么

### 第一层：工具健壮性（Commit 1–5）

1. **ToolResult 类型**：`execute()` 的返回值从裸字符串升级为 `ToolResult(content, is_error)`，让"正常结果"和"工具失败"在类型层面就能区分——这是模型后续能"失败重试"的基础。
2. **异常统一处理**：删掉每个工具里各自的 `try/except`，让异常自然抛出，由 AgentLoop 在唯一一处兜底转换成 `ToolResult(is_error=True)`。错误处理不再散落各处。
3. **参数校验**：`validate_arguments()` 只做"必填 + 类型"两项自检，把 `unsupported operand type(s) for /: 'WindowsPath' and 'int'` 这类隐晦的 Python 内部报错，翻译成模型能直接行动的 `path should be str, got int`。
4. **超时机制**：工具在独立线程执行，主线程用 `future.result(timeout=...)` 限时等待——Agent 第一次拥有"不会因为工具死掉"的能力。
5. **Tool metadata**：`timeout` 和 `dangerous` 两个字段挂到 Tool 基类，为未来的权限控制、用户确认、sandbox 预留挂点。

### 第二层：Runtime 化（Commit 6–9）

6. **事件流取代 print**：AgentLoop 里所有 `print()` 全部移除，改为产出 `AgentEvent`。CLI、未来的 Web、日志都是同一份事件流的消费者——一个核心，多种前端。
7. **yield 生成器架构**：`run()` 变成生成器，逐个 yield 事件，消费者用 `for event in agent.prompt(...)` 拉取。核心代码与显示彻底解耦。
8. **step() 拆分**：把 `run()` 里"循环 + 单轮逻辑"拆开——`run()` 退化成反复调 `step()` 的薄壳，单轮逻辑独立成可单独调用的生成器。从"一口气跑完"变成"可单步调试"。
9. **AgentState 状态机 + abort()**：`IDLE / THINKING / CALLING_TOOL / FINISHED / ERROR` 五个状态，外部随时可查 `agent.state`；`loop.abort()` 在安全检查点协作式中止，Ctrl+C 不再杀死 REPL，而是让 Agent 安全退出当前任务。

## 三、踩过的坑（最有价值的部分）

### 1. ThreadPoolExecutor 的 `with` 陷阱

第一版超时实现用了 `with ThreadPoolExecutor(...) as executor:`，测试才发现：`with` 块退出时会调用 `shutdown(wait=True)`，**阻塞等待那个卡死的工具线程**——超时形同虚设，卡死依旧卡死。正确写法是 `finally: executor.shutdown(wait=False)`，主流程立即放手。

### 2. Python 线程无法强杀

超时只是让主流程继续，那个卡死的工具线程会一直在后台跑，直到进程退出才被清理。这是 Python 线程的固有限制，只能接受并如实告知——bash 工具这类真卡死靠的是它内部 `subprocess.run(timeout=...)` 真正杀掉子进程，线程超时只是兜底。

### 3. yield vs emit：一个被跳过阶段的决策

第三阶段的计划是给 `AgentLoop` 加 callback/subscriber 支持。分析后发现：**callback 是 yield 生成器的弱子集**——任何回调能做的事，`for event in run(): handle(event)` 一行就能实现，反过来却不行。选择了更强的架构，直接跳过该阶段。架构选型时"选更灵活的那个"往往是对的。

### 4. 参数校验的"意义之问"

开工前用户问了一个好问题：既然异常处理已经把崩溃全兜住了，为什么还要校验参数？实测数据说话：`path=123` 时模型收到的是 `unsupported operand type(s) for /: 'WindowsPath' and 'int'`——没有出现参数名，模型得自己猜错在哪。校验的唯一实质收益不是防崩溃，而是**把隐晦报错翻译成可行动的提示**。

### 5. 双层超时的归一

bash 工具内部硬编码 `subprocess.run(timeout=30)`，新 metadata 声明 `timeout=60`，loop 线程超时还是全局 30s——三层不一致。最终把超时收进 Tool 的 `timeout` 字段：loop 用 `tool.timeout`，subprocess 也用 `self.timeout`，metadata 成为唯一真相源。

### 6. 状态机的一个小语义 bug

初始实现里 `yield turn_start` 之后才设 `state = THINKING`，导致消费者收到 `turn_start` 事件时看到的还是旧状态。修法是把状态切换放到 yield 之前——**状态应该在事件被观察时就已经准确**。

## 四、当前的位置与下一步

今天的分界线是：从"脚本"到"runtime"。现在 AgentLoop 是可复用的核心——事件流、单步、状态机、中止，四件套齐了。

下一步方向：
- **审批门**：`dangerous=True` 字段的消费者，在 `step()` 拆出后可以插入"危险工具需用户确认"
- **上下文压缩**：超长对话自动总结（pi 的 compaction 设计）
- **产品化**：`--workspace` 参数解绑硬编码目录 + `pyproject.toml` 打包成全局命令（像 claude/pi 一样任意目录可启动）
