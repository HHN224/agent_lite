# DeepSeek Harness (DSH) — 上下文管理设计调研

> 来源：本地安装的 DeepSeek Harness 源码（`node_modules/@deepseek-ai/dsh` 及其 `@deepseek-ai/dsh-*` 子包）。
> 这些 README 是 DSH 打包分发包自带的权威说明，直接反映其真实架构。
> 调研时间：本项目研发阶段。文中「DSH」均指 DeepSeek Harness。

这个模块是本项目 agent_lite 最值得借鉴的「上下文管理」范本，因为它把**上下文管理拆成了可独立替换的多个小队（seam）**，而不是塞进一个类里。

---

## 0. 总体心智模型：事件溯源 + Surface 投影 + Compaction 引擎

DSH 上下文管理的核心是 **session（对话日志）与模型消息历史不是一回事**。

```
append-only 事件日志 (Session)     ←—— 唯一事实源，raw 全量保留
        │  派生 / 投影
        ▼
Surface（有序的"消息生产事件"投影） ←—— 模型真正看到的对话历史
        │  派生
        ▼
deriveMessages() → 发给模型的 messages[]
```

关键推断：
- **会话日志是 append-only 的**，每一轮只追加，从来不改写旧条目。
- **模型的消息历史是从日志「派生」出来的**，不是直接存的。
- 一个 **surface 层**（有序的消息生产事件投影）架在 raw log 之上，为了高效派生与压缩。
- **Compaction（压缩）就是往 surface 上做一次「替换/遮盖（replace）」**：把一段旧的历史区间替换成一条摘要节点。被遮盖的旧 raw 事件**仍然留在日志里**（可重放、可审计），只是不再出现在派生消息里。
- **surface 上只有三种事件能承载 `surfaceOp`**（`user/message`、`assistant/message`、`tool/result`），`compaction/*` 事件是「纯日志」事件，绝不出现在 surface 上。

这与 agent_lite 现有 `session.py` 的 `SessionEntry.type == "compaction"` + `first_kept_entry_id` + `build_llm_payload()` 的**接缝方向完全一致**——只是 agent_lite 目前用「线性链 parent_id」而非「事件溯源 + surface 替换」。

---

## 1. 能力被拆成 4 个可替换的包（seam 模式）

DSH 把「压缩」这个能力拆成 角色，各自可独立演化和替换：

| 包 | 角色 | 干什么 |
|---|---|---|
| `dsh-compaction` | **Service Definition（抽象定义）** | 定义 `ctx.compaction` 的三个操作 + `compaction/*` 事件 + `CompactionResult` + 工具配对边界助手。只定义「压缩是什么」，不定义「怎么做」。 |
| `dsh-compaction-basic` | **Service Provider（具体后端）** | 用 `ctx.tokenMeter` 测压力 + token-budget 保留策略 + `llm.stream()` 直接做摘要。这是默认实现。 |
| `dsh-compaction-tool-result-pruner` | 可选「剪枝小队」 | 在压缩前先**无模型**地重写过长的 tool result（留头+省略标记+留尾），避免把大段工具输出喂进摘要。 |
| `dsh-command-compact` | **消费者（人类命令）** | `/compact` 命令，调用 `ctx.compaction.compactNow()`。 |
| `dsh-token-meter` | **独立计量服务** | 用固定启发式（约 4 字符/token + 结构开销）测压力，不依赖压缩引擎，可被任何压力敏感插件共享。 |

**设计要点**：抽象定义（`dsh-compaction`）只依赖 `dsh-session` 和 `dsh-llm`（因为「压缩」的动词是定义在 Session 上的，输出是 ContentBlock 词汇）。这是对「Service Definition 只依赖 cordis」这条通用规则的**有意偏离**，已在 DSH 的 Agent Note 里记录。

> 对 agent_lite 的启发：不要做一个「大而全的 ContextManager」，而是「一个抽象的压缩引擎接口 + 一个默认实现 + 一个可选剪枝器 + 一个计量器」，各自可单独替换/组合。

---

## 2. Abstract Service API（`ctx.compaction`）

三个操作都是**抽象**的，触发策略、保留策略、事件顺序、摘要生成全部由后端决定：

| 成员 | 语义 |
|---|---|
| `compactIfNeeded(agent, trigger, signal)` | 自动压缩的入口。`trigger: 'pressure'`（压力触发）或 `'context-overflow'`（确认溢出）。pressure 可用后端阈值和保留尾部策略；confirmed overflow 可强制做一次有用的均衡缩减。无安全区间时返回 `null`。注意：后端做摘要时是直接调 `ctx.llm.stream()`，**不是走 loop 的 step**，所以逐调用拦截发生在 `llm/stream`。 |
| `compactNow(agent, signal)` | 显式压缩一次「有用均衡的较旧跨度」（即使没到自动压力线）。同步预留空闲轮次后才 yield；没有可用跨度时不写任何东西；记一个独立的 `compaction/* {turn:null}`。手动操作，不要求有打开的 turn。 |
| `compactRegion(start, end, agent, signal?)` | 强制把 surface 上 `[start,end]` 区间（含）的节点压缩成一条替代节点。若已有压缩在进行、或 start/end 不是 surface 节点、或 start 在 end 之后，则**抛异常**。 |

`CompactionResult` 保留原始摘要、簿记事件 seq、被遮盖区间、token 记账。

`compactIfNeeded` / `compactNow` 需要 `signal`；`compactRegion` 的 signal 可选。后端若是用 `ctx.llm.stream()` 做摘要，**必须**把 signal 传进 `GenerateOptions.signal`，这样 abort 或 fiber dispose 会拆掉进行中的摘要调用。

> 对 agent_lite 的启发：压缩引擎接口应当接受一个 `AbortSignal`（Python 里可用 `threading.Event` / asyncio 取消），并且摘要调用要能真正被取消。

---

## 3. Surface 契约与「替换」是怎么落地的（重点）

一次成功的压缩按固定顺序落一堆**日志事件**：

1. `compaction/start`（纯日志）—— **获取锁**。
2. 摘要那段区间。
3. `compaction/summary`（纯日志）—— 带摘要、区间、被遮盖 seq、token 数、provider/model 调用信封。
4. **只有这一个 surface 变更**：追加一条 `user/message`，带 `source: compactCheckpointSource(compactionId)` 和 `surfaceOp: { op: 'replace', start, end }`，内容就是摘要。
5. `compaction/end`（纯日志）—— **释放锁**。

要点：
- **surface 变更（第 4 步）是唯一一次真正改 surface 的地方**，它被夹在 lock bracket 内。`compaction/end` 是最后一个事件，所以锁绝不会在变更落地前被释放。
- 一条 `compaction/start` 没有匹配的 `compaction/end` → 说明中间崩溃，留下一个「可检测的孤儿锁」，而不是一个「谎称压缩完成但 surface 没被遮盖」的 `compaction/end`。
- `deriveMessages()` 把摘要渲染成一条 **user role 的消息**，后面跟保留节点。**被遮盖的旧事件留在 raw log 里**，所以重放是确定性的。
- 锁是**持久的 bracket**（`compaction/start`/`compaction/end` 两个日志事件），不是 `WeakSet`、wrapper mutex 或客户端锚点。`compaction/start` 是在摘要 yield 前**同步**追加的。后续任一失败只做**一次** `compaction/end {error}` 尝试；如果这次关锁本身失败，那个未匹配的 start 就**故意**保持为「忙碌」信号，不做 flush。

**锁不跨 turn**：一个活跃的 bracket 不能跨越 `turn/start` / `turn/end`。

> 对 agent_lite 的启发：agent_lite 的数据层用 `parent_id` 链 + `head` 做得不错；但 DSH 展示了更稳健的「压缩 = 一次带锁的 surface 替换，且锁本身是持久化的日志事件」。这能处理崩溃恢复与并发。

---

## 4. 具体后端：`dsh-compaction-basic` 的策略

这是默认实现，它的核心**策略**包括：

### 测量（Measurement）
- 用单例 `ctx.tokenMeter` 给「最新 canonical 请求信封」和「当前 surface」定价，在**同一份消费掉的日志版本**上做。
- 所以 step 边界的压力**包含了**真实的 system prompt、tools、路由、assistant 完成、工具结果、缓冲上下文、steering。

### 路由策略（Routed policy）
- 主动压力用「最新持久 provider/model 路由」所属的 adapter 来解出容量（capacity），再按**默认策略 + 可选的精确目标覆盖**换算成具体 token 预算。
- 模型发现（listModels）只是参考，**不参与决策**。

### 模型无关剪枝（Model-free pruning）
- 压力或 canonical 溢出合格后，可选 `ctx.toolResultPruner` 先重写过长的 tool result，再做区间选择。
- 减完再通过 `ctx.tokenMeter` 重新量测；若压力已安全，就**跳过摘要**；否则就摘要剪枝后的 surface。
- 低于压力的 step 检查**永不剪枝**。

### 保留策略（Retention）
- 压缩**最旧**的完整 surface 单元，同时保留**最近的尾部**，并通过 `dsh-compaction` 的**工具配对边界助手**保证「切点不在一个未回复的 assistant tool call 上」。
- turn 边界不保护一个疯狂 turn 里的旧 step。
- 一个打开的不可分割尾部（indivisible tail）会**拒绝**，直到它关闭。
- 可选剪枝器能修复一个过长的封闭工具单元（当它「含文本的结果」是可移除的大块时）；不可分割的非工具单元、或非可剪枝的工具余量，仍然超出范围。

### 收敛（Convergence）
- 重试「头部检查点」压缩最多 `compactionRetries` 次。
- 拒绝一个**没有缩小**源的摘要；若重试仍无法低于阈值，则抛异常。

### 摘要（Summarization）
- 直接一次 `llm/stream` 调用，用配置的 provider/model/cap；缺省时回退到「最近一次已记录的请求目标」，再回退到 AgentOptions 的 provider/model。**不**走 loop 专用的 `agent/request` 扩展点。
- **关键（KV cache 友好）**：它**回放**对话自己的 system prompt、tools、被遮盖区间的消息（原封不动，含图片引用），然后追加压缩指令作为最后一条 user 消息。这样**复用 provider 的 warm prefix cache**，而不是失效它。
- 设置 `GenerateOptions.purpose = 'compaction'`，供 adapter 做请求归因（DeepSeek adapter 发 `x-deepseek-harness-compact: 1`），但不触碰模型可见 body。
- **只返回文本**进入检查点：排除 reasoning 和 tool calls（会泄露私有推理，或产生一个孤儿调用）。图片输出失败返回 `UNSUPPORTED_CONTENT` 而不是消失。

### Framing（框架）
- 替代的 user 消息用 `<compacted-summary>` 标签标记已建立的检查点上下文。原始摘要保留在 `compaction/summary` 事件上。后面的自动循环会把前面的检查点**合并**掉。

### 溢出恢复（Overflow recovery）
- provider 确认的溢出**不需要**容量元数据：绕过正常压力和保留，先剪枝，然后尝试一次「最大的均衡头部缩减」，同时留下最新不可分割单元。
- **只要 `surface.replaceGeneration` 前进就当重试被授权**，包括后来摘要抛异常但剪枝已经落地的情形。无替代、目标特定 cap 耗尽、取消、或未知/非 canonical 错误 → 保留原始 provider 失败。

---

## 5. 配置项（`BasicCompactionConfig`）

每个设置都可选。顶层字段是**每个路由模型的默认值**；`modelPolicies` 对精确的 provider/model 对做**局部覆盖**。压力时，DSH 请对应适配器「该路由的上下文容量」，再解析出绝对预算。

| Key | 默认 | 含义 |
|---|---|---|
| `thresholdRatio` | `0.8` | 当 `floor(路由上下文窗口 × ratio)` 时压缩。 |
| `retainRatio` | `0.16` | 保留的最近 surface 预算 = 路由窗口 × 比率。与 `retainTokens` 互斥。 |
| `retainTokens` | — | 保留的最近 surface 绝对预算。必须低于解析出的阈值。 |
| `summarizationProvider`/`Model` | `''` | 摘要调用用的 provider/model；空时解析「最近请求目标 → AgentOptions」。 |
| `maxTokens` | `8192` | 摘要调用生成的 cap（可能含推理 token）。 |
| `compactionRetries` | `1` | 压力仍高于阈值时的额外尝试。 |
| `maxOverflowRetries` | `1` | canonical 上下文窗口溢出后的最大重试；`0` 只关闭恢复。 |
| `modelPolicies` | `[]` | 精确 `{provider, model, ...partial}` 覆盖。 |
| `auto` | `true` | 注册 step 边界压力与溢出恢复监听器；`false` 仅手动。 |

> 对 agent_lite 的启发：默认 `thresholdRatio: 0.8`、`retainRatio: 0.16` 是很有用的「起始点」。保留最近的 16% 窗口原文，其余做摘要——这是很多厂商共同的做法。

---

## 6. 摘要指令模板（可直接参考）

DSH 的摘要 call 最后一条 user 指令给出**固定 Markdown 结构**，要求用点式 bullet、不丢 section、空 section 写 `(none)`、保留原字符串。这是一份质量很高的检查点模板：

```markdown
You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
## Key Technical Concepts
## Files and Code
## Errors and Fixes
## Pending Jobs
## Current Work
## Next Step
## Critical Context
```

几条关键规则：
- 保留精确文件路径、命令、错误串、标识符、数值、函数签名、语法片段。
- 忠实记录用户反馈和明确指示，特别是**纠正**。
- **不要提**这是一次压碎请求、不要提上下文被 compacted。
- 如果对话里已有 `<compacted-summary>` 块，那是**旧的检查点**：**不要照搬前进**，保留仍为真的事实、丢弃已过时的、合并新信息成一个合并的摘要。

Checkpoint preamble（检查点前导）：

```markdown
This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context. Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint.
```

---

## 7. Token 计量：`dsh-token-meter`

- 单例 `ctx.tokenMeter`，无配置项。用**一个固定启发式**：`4 字符/token + 结构开销（role、block、request 信封字段）`。拒绝任何 key。
- `measure(session, requestHeader?)` 返回请求压力和当前定价的 surface；`estimateMessage(message)` 定价单条消息。
- `measure()` 同步化一次，返回一份**分离、深不可变**的快照。`totalTokens` 是请求+响应压力；`surfaceTokens` 是 surface-only 启发式总量，等于 `nodes[].tokens` 之和。每次克隆位置节点，所以测量是 O(surface)。
- **provider usage 复用的条件很苛刻**：只有当「最近一次成功调用的 canonical 请求信封」与「当前被测量的信封」**完全一致**，且其总量不低于该调用的完整启发式锚点时，才复用 provider usage；否则整体用启发式估算。后面的成功替换前面的锚点。
- usage 记账把 input、cache-read、cache-write、output 各自相加（互不相交）；reasoning 不重复加。
- 投影：`tokenUsage`（全量 uncachedInput/output/cacheRead/cacheWrite）、`contextPressure`（`pressureTokens`=provider 报告的最新 prompt 大小、`projectedTokens`、`contextWindow`）、`contextBreakdown`（systemTokens/toolsTokens/messageTokens 的启发式构成）。

**重要提醒——关于占用率：** `contextPressure` 等占用字段是**独立的 last-wins 记录，不是一次请求的原子观察**。换模型会拿「新容量」配「上一条路由的样本」，直到下一个请求上报 usage。占用百分比是**用户可读的参考值，不是计费记录，也不是门控输入**——DSH 里没有任何东西靠它做决策，压缩读的是 `measure()`。UI 用「measured pressure ÷ 单独解析的容量」算占用率。

> 对 agent_lite 的启发：agent_lite 的 `estimate_tokens()` 已是 `chars/4` 启发式（并有 `_token_estimate` 可注入替换）。DSH 的做法证明这就够了——**关键是让「计量」与「压缩引擎」解耦**，并明确「占用率只是展示，不做决策依据」。这也是为什么 agent_lite 现在 `estimate_tokens` 已经是独立、可注入钩子。

---

## 8. Tool Result 剪枝器：`dsh-compaction-tool-result-pruner`

模型无关的剪枝服务，**不是压缩后端，也不是模型可见的工具**。`compact-basic` 通过可选 `ctx.get('toolResultPruner')` 读它，所以两者可独立组合。

- `pruneSession(session)`：扫描当前 surface 的快照。每个 over-budget 的 tool result 被替换成**新追加的**一条 `tool/result`，带 `surfaceOp: {op:'replace', start:originalSeq, end:originalSeq}`、`sourceEventSeqs:[originalSeq]`。替换**展开完整原始数据，只改 `content`**，保留 `turn`、`step`、`callId`、错误字段、`meta`。**原始事件仍保留**在 append-only 日志里供持久化/重放/精确检视。
- `measureContent(blocks)`：数 text block 的 Unicode 码点。
- `pruneContent(blocks)`：返回有界替换；content 已在阈值内则返回 `null`。非文本块按原相对位置保留；文本切片不拆 UTF-16 代理对，但可能拆多码点字素簇。

配置（默认）：

| Key | 默认 | 含义 |
|---|---|---|
| `thresholdChars` | `8192` | 合并文本超过这个码点数就剪枝。 |
| `headChars` | `4096` | 保留的头部码点数。 |
| `tailChars` | `1024` | 保留的尾部码点数。 |

模型看到的是：
```
<保留的头部>  \n\n[... tool result middle pruned ...]\n\n  <保留的尾部>
```

模型看不到原始的第二份副本。剪枝本身不产模型调用；`compact-basic` 在重测压力低于阈值时跳过摘要，否则摘要读取剪枝后的 surface。

> 对 agent_lite 的启发：**先无模型剪枝、再决定要不要摘要**，是省钱的好办法。默认 `8192 / 4096 / 1024` 可作起点。

---

## 9. Surface 上的保留 + 工具配对边界（tool-pairing）

`dsh-compaction` 导出 `toolPairingBalancedBefore(session, seq)` / `toolPairingBalancedAfter(session, seq)` 用于「吸附与校验压缩边」。一个安全的边（cut）上**不能有未回复的 assistant tool call 穿过它**。只有当前 surface 里的事件序列才算，且从「按 surface 顺序缓存的余额」回答。

**为什么重要**：如果压缩区间中间切在「assistant 发起了一个 tool call，但还没 tool/result 回复」的地方，模型历史就会缺配对出现损坏。所以压缩的 cut 必须落在「已经配平的 tool 区间」上。

> 对 agent_lite 的启发：agent_lite 目前用线性消息链，无「配对」概念。做压缩时**必须保证 cut 不落在未闭合的工具调用区间内**（assistant 带 tool_calls 但对应 tool 消息未回填），否则重启恢复的模型历史会坏。这是 DSH 里一个重要的、容易被忽略的正确性要点。

---

## 10. 会话持久化（JSONL）与模型历史重建

DSH 默认用 `dsh-session-persistence-jsonl`：

```
<root>/--<normalized-cwd>--/<encoded-id>/session.jsonl.zstd
```

- 每个 session 一条 append-only 逻辑 JSONL 日志，默认 `.jsonl.zstd`（zstd + 校验头帧），也可关闭压缩用 raw `.jsonl`。
- 首行是**不可变 `SessionHeader`**（`version/id/cwd/createdAt/parentSession/seedLength/origin/delegationDepth/agentPreset`）。
- 后续每行是一条 `SessionEvent` JSON 原样，或——对可打包运行的——一条**packed chunk row**（把一长串 ≥2 的连续同块 `assistant/chunk` delta 事件打包成一行，约省 60% 大小）。
- **崩溃恢复**：`load` 校验每个完整压缩帧；若最后一帧结构不完整，保留其完整解码记录，从该帧起点截断，并用共享持久化契约要求的合成 tool/step/turn 关闭器重新编码。raw 模式从第一行不完整处截断。
- `assistant/chunk` 原始记录**不重复**产生 message。
- **恢复的模型历史**：加载恢复存储的 surface 历史，保留先前的 request headers 供重建；新 loop 组装当前信封。一个「有 assistant 请求但无持久调用」的被平衡为 `TOOL_NOT_STARTED`；一个「有持久调用但无结果」的变成 `TOOL_OUTCOME_UNKNOWN`——这告诉模型**只重试只读/幂等工作，并验证可能的副作用或询问用户**。

> 对 agent_lite 的启发：agent_lite 的 `SessionRepository` 存的是 `sessions/<id>.json`（整份 JSON，原子替换 tmp）。DSH 用「append-only JSONL(zstd) + 崩溃恢复截断 + 合成关闭器」更健壮。但 agent_lite 的「整份 JSON 原子写」对教学项目已够用。**值得借鉴的一点**：恢复时若发现「工具调用已发起但无结果」或「请求已发出但无持久调用」，应合成状态告诉模型「只重试只读/幂等，并验证副作用」。这对 agent_lite 的 `/compact` 恢复和中断恢复都很有用。

---

## 11. LLM 装配与消息派生（`dsh-llm` / `dsh-session`）

- `ctx.llm` 是 **provider-neutral** 的 LLM 词汇 + 抽象服务。它定义 agent loop、session log 和每个插件说的「标准语言」。消息是共享不可变值（`MessageId`、role、content、typed source）。
- 消息内容是一个**类型化 block 数组**：`text`、`reasoning`、`image`、`tool-call`、`tool-result`。union 通过 `ContentBlockMap` 可合并扩展。
- `BlockAssembler`：增量把 raw chunk 组装成完整 ContentBlock + 最终 assistant Message。agent loop 喂它（同时记录 raw chunks 供重放保真），流结束后读 `blocks()`/`message()`/`usage`/`finish`，或被取消时读 `interruptedBlocks()`。
- **max-token 截断会丢弃「无法安全执行的 tool call」**。`interruptedBlocks()` 只保留「已关闭/打开的 text/reasoning 块且有非空白内容」，**tool calls 被省略**（因为中断先于分发，保留其一则需要一个伪造的结果）。

- `session.deriveMessages()` 增量投影每个新的 surface entry 一次，返回一个**新的数组**，包含完整的、已识别的、冻结的消息。assistant 消息保留产它的 provider/model + adapter 私有重放状态。**surface 重写会重建投影；没有 raw-log 回退。**
- `session.surface` 暴露只读 `SessionSurface` 视图；`replaceGeneration` 每次提交的 rewrite 都会变。
- 一个**人类用户看到的 transcript** 必须投影「append-origin 事件」而不是 `session.surface`——因为落地的替换会遮盖「读者已经看到的」历史；而**模型面**的消费者继续读 `session.surface`。

> 对 agent_lite 的启发：DSH 区分「人类 transcript」（用 append-only 事件投影，遮盖的历史仍可见）与「模型消息历史」（用 surface，遮盖的历史不可见）。agent_lite 现在 `build_llm_payload()` 直接给出模型历史；如果以后要有 UI，需注意这条区分。

---

## 12. 已知限制与边界（哪些不算 surface-compaction 的活）

DSH 明确写出几个**不归上下文压缩管**的边界，避免过度设计：

- **系统 prompt、tools、session prefix 不归压缩管**——一个「单靠 envelope 就接近窗口」的情况不是 surface-compaction 的活。
- **某些单单元溢出超出契约**：均衡摘要压缩无法拆一个不可分割单元。可选剪枝器能修复「已闭合的 tool 区间」当含文本的 tool-result 是可移除大块；但一个大的非 tool 节点、或一个「非可剪枝余量仍超额」的 tool 单元无法压缩。
- **人类命令，不是模型工具**：`/compact` 是 `ctx.commands` 暴露的，**没有注册模型可见的压缩工具**。
- **计量准确度随固定启发式**：缺失可复用 provider usage 时回退到字符计数 + 结构开销，而非精确 tokenizer。
- **溢出分类由 adapter 维护**：provider 措辞可能变化；两个 DeepSeek adapter 目前把已识别的 context-limit 失败归一化为 `CONTEXT_WINDOW_EXCEEDED`。
- **`compactRegion` 需要打开的 turn**：对已完全关闭的 session 手动调用会抛「no open turn」而非压缩。
- **摘要失败保留最新耐久 surface**：在任何替换前，auto 路径记警告并以完整的超预算历史继续；若剪枝已落地，则从那份耐久剪枝 surface 继续。

---

## 13. agent_lite 可直接落地的借鉴点（结合现有代码）

对照 agent_lite 现有 `session.py`：

1. **压缩 = 一次带锁的 surface 替换**。agent_lite 已有 `append_compaction(summary, first_kept_entry_id)` + `build_llm_payload()`（把 summary 作为第二条 system 消息注入 + 从 `first_kept_entry_id` 取保留消息）。这与 DSH 的「摘要作为 user 消息 + replace 区间」方向一致，但 agent_lite 目前把摘要当**第二条 system 消息**、且保留从 `first_kept_entry_id` 开始——**没有真正"遮盖"旧的 raw 消息**，只是「从某个点开始不发了」。这是 DSH 与 agent_lite 的一个结构性差异：DSH 用 `surfaceOp.replace` 真正把旧区间从 surface 上拿掉；agent_lite 用「起始指针」模拟。两者各有利弊，agent_lite 更简单、更易重放，但「头部检查点压缩」会越攒越多。

2. **切点必须工具配对平衡**（§9）。这是 agent_lite 目前完全缺失的正确性护栏。

3. **默认策略**：`thresholdRatio 0.8`，`retainRatio 0.16`（或 `retainTokens` 绝对预算），`maxTokens 8192`，`compactionRetries 1`，`maxOverflowRetries 1`。

4. **先无模型剪枝 tool result 再决定摘要**（§8），阈值 `thresholdChars 8192`、`head 4096`、`tail 1024`。

5. **摘要用「回放对话 + 追加指令」以复用 KV cache**（§4）。agent_lite 如果要复用 provider 的 prefix cache，摘要调用应**重发**原 system prompt + 工具 schema + 被遮区间消息，再追加摘要指令，而不是只发「当前最近几条」。

6. **摘要指令用固定结构**（§6），且「已有旧检查点就合并、不照搬前进」。

7. **计量与引擎解耦**，计量是 `chars/4` 启发式（agent_lite 已是），占用率只做展示不做决策（§7）。

8. **恢复/中断时合成工具状态**（§10）：已发起无结果的 tool call → `TOOL_OUTCOME_UNKNOWN`（只重试只读/幂等、验证副作用或问用户）；已发出无持久调用的 assistant 请求 → `TOOL_NOT_STARTED`。

9. **区分「模型历史」（surface，遮盖不可见）与「人类 transcript」（append-only，遮盖仍可见）**——若将来有 UI。

10. **把能力拆成可替换小队**（§1）：一个抽象接口（`CompactionEngine`）+ 一个默认实现 + 一个可选剪枝器 + 一个计量器，而不是一个巨型 `ContextManager`。

---

## 14. pi-agent 的 token 估算与溢出检测实现（DSH 内置 `@earendil-works/pi-ai`）

agent_lite 复刻的正是 pi-agent；DSH 内部也打包了 `@earendil-works/pi-ai`，其 `dist/utils/estimate.js` 与 `dist/utils/overflow.js` 给出了**可直接参照的 token 估算与上下文溢出检测**实现。

### Token 估算（`estimate.js`）

- `CHARS_PER_TOKEN = 4`；`ESTIMATED_IMAGE_CHARS = 4800`（一张图按 4800 字符 ≈ 1200 token 估计）。
- `estimateTextTokens(text) = Math.ceil(text.length / 4)` —— 纯文本启发式。
- `estimateTextAndImageContentTokens(content)` 把 content（字符串或 content block 数组）里 text 块长度累计、image 块按 4800 字符计，再 `ceil(总字符/4)`。**注意 content 里 image block 单独按 ESTIMATED_IMAGE_CHARS 算**。
- `estimateMessageTokens(message)`：
  - `user` / `toolResult` → 直接 `estimateTextAndImageContentTokens(message.content)`。
  - 其它（assistant 等）→ 遍历 content blocks：`text` 加 `text.length`，`thinking` 加 `thinking.length`，否则（tool-call）加 `name.length + JSON.stringify(arguments).length`，再 `ceil(total/4)`。
- `estimateToolsTokens(tools) = estimateTextTokens(JSON.stringify(tools))` —— 工具 schema 走 JSON 字符串长度/4。
- **核心策略——优先复用上次 provider usage**：`estimateMessages(messages)` 先 `getLastAssistantUsageInfo(messages)` 找**最新的、未被更新的前缀消息遮挡的** assistant message（`timestamp >= latestPrefixTimestamp`、`stopReason` 不是 aborted/error、且 `usage` 的 input+output+cacheRead+cacheWrite > 0）。找到则：
  - `tokens = usageTokens + trailingTokens`（`usageTokens` 用 provider 报的真实值，`trailingTokens` 是 usage 索引之后消息的启发式估算之和）。
  - 找不到才退回「全部启发式估算」：`tokens = sum(estimateMessageTokens(messages))`。
- `estimateContextTokens(context)`：若 context 是 message 数组直接 estimateMessages；否则（有 systemPrompt/tools 的结构）：

  ```
  prefixTokens = estimateTextTokens(systemPrompt) + estimateToolsTokens(tools)
  tokens = estimate.tokens + prefixTokens   // 把 system prompt + 工具 schema 也算进 context 成本
  ```

> 对 agent_lite 的启发：agent_lite 的 `Session.estimate_tokens()` 目前是 `max(1, len(text)//4)`，**没有考虑**：图片（2800/4800 字符）、工具 schema、以及「最新一条 assistant message 的 provider usage 可复用」。若要更省、更贴近真实成本，可以把这三点加进 agent_lite 的估算。特别是「**优先复用 provider 返回的真实 usage**，只在 usage 之后的新消息用启发式估算」——这是 pi-agent/DSH 的做法，能显著提高上下文占用估算的准确度。

### 上下文溢出检测（`overflow.js`）

`isContextOverflow(message, contextWindow)` 检测三种溢出：

1. **基于错误消息**（大多数 provider）：若 `stopReason === "error"` 且有 `errorMessage`，且**不匹配非溢出**模式（`NON_OVERFLOW_PATTERNS`：rate limit、too many requests、Throttling/Service unavailable 前缀——避免把限流误判为溢出），但**匹配任一 `OVERFLOW_PATTERNS`**（一个覆盖 Anthropic/OpenAI/Google/xAI/Groq/OpenRouter/Together/llama.cpp/LM Studio/DS4/Mistral/DashScope/Ollama 等的繁正则清单），则判定溢出。
2. **静默溢出**（z.ai 风格）：`stopReason === "stop"`（成功）但 `usage.input + usage.cacheRead > contextWindow`。
3. **长度截断溢出**（Xiaomi MiMo 风格）：provider 悄悄把输入截断到 contextWindow、`stopReason === "length"` 且 `usage.output === 0`、且 `input+cacheRead >= contextWindow*0.99`。

`OVERFLOW_PATTERNS` 里的示例错误串非常有价值（DSH 实际收集到的），例如：

- Anthropic: `"prompt is too long: 213462 tokens > 200000 maximum"` / HTTP 413 `request_too_large`
- OpenAI: `"Your input exceeds the context window of this model"` / `"exceeds the model's maximum context length of X tokens"`
- Google Gemini: `"The input token count (1196265) exceeds the maximum number of tokens allowed (1048575)"`
- xAI: `"maximum prompt length is X but the request contains Y tokens"`
- DS4: `"Prompt has X tokens, but the configured context size is Y tokens"`、`"Input length (265330) exceeds model's maximum context length (262144)."`

> 对 agent_lite 的启发：agent_lite 目前**没有溢出检测**。做上下文管理时，`/compact` 与自动压缩通常需要区分「**接近窗口（pressure）→ 主动压缩**」与「**已确认溢出（overflow）→ 强制恢复**」。把上面的正则清单（或精简为 OpenAI/DeepSeek/Anthropic 三条主力）作为 `is_context_overflow()` 的起点，能直接复用。

---

## 参考资料（DSH 本地源码）

- `@deepseek-ai/dsh-compaction` README（Service Definition / surface 契约 / 锁 / 工具配对边界）
- `@deepseek-ai/dsh-compaction-basic` README（策略 / 配置 / 摘要指令模板 / 溢出恢复）
- `@deepseek-ai/dsh-compaction-tool-result-pruner` README（剪枝器）
- `@deepseek-ai/dsh-token-meter` README（计量 / 项目化 / 占用率近似性）
- `@deepseek-ai/dsh-command-compact` README（/compact 人类命令）
- `@deepseek-ai/dsh-session` README（事件溯源 + surface / 派生）
- `@deepseek-ai/dsh-session-persistence-jsonl` README（JSONL 持久化 / 崩溃恢复）
- `@deepseek-ai/dsh-llm` README + `assembler.d.ts`（消息 block / BlockAssembler / KV cache 感知）
- `@deepseek-ai/dsh-agent` + `@deepseek-ai/dsh-agent-loop` README（loop 事件体系 / 压缩属于插件 / KV cache 效应）
