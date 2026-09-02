# Claude Code / Claude Agent SDK — LLM 上下文管理调研

> 调研目的：为 agent_lite 的上下文管理模块提供参考。
> 来源标注：每条结论区分**OFFICIAL**（Anthropic 官方文档/博客）与**LEAKED SOURCE**（2026-03-31 泄漏的 `@anthropic-ai/claude-code@2.1.88` sourcemap，约 51.2 万行 TS，已获 Anthropic 确认为"发布打包失误，非安全漏洞"）。
> ⚠️ **重要**：泄漏源码来自特定版本（2.1.88），常量可能随版本漂移（Anthropic 官方承认）；引用常量时均标注来源文件。**不要把这些精确数字当作圣经**，应针对你自己的模型/tokenizer 重新标定。

---

## Topic 1 — 何时触发压缩（自动压缩决策逻辑）

**机制**：自动压缩由 **headroom 记账**驱动，而不是固定"token 阈值"（尽管底层会算出一个有效窗口常量）。见泄漏 `autoCompact.ts`。

### 有效上下文窗口
```ts
function getEffectiveContextWindowSize(model) {
  const reservedTokensForSummary = Math.min(getMaxOutputTokensForModel(model), 20_000)
  let contextWindow = getContextWindowForModel(model, getSdkBetas())
  const autoCompactWindow = process.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW
  if (autoCompactWindow) contextWindow = Math.min(contextWindow, parseInt(autoCompactWindow, 10))
  return contextWindow - reservedTokensForSummary
}
```
- `MODEL_CONTEXT_WINDOW_DEFAULT = 200_000`（泄漏 `src/utils/context.ts`）。
- `getContextWindowForModel()` 解析 `[1m]` 后缀 → 1,000,000，否则用模型能力，否则 200,000。`CLAUDE_CODE_DISABLE_1M_CONTEXT=1` 强制 200K。
- 输出保留上限 = `min(maxOutputTokens, 20_000)`；注释说"基于压缩摘要输出的 p99.99 为 17,387 tokens"。

### 阈值常量（泄漏 `autoCompact.ts`）
```ts
export const AUTOCOMPACT_BUFFER_TOKENS = 13_000
export const WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
export const ERROR_THRESHOLD_BUFFER_TOKENS  = 20_000
export const MANUAL_COMPACT_BUFFER_TOKENS   = 3_000
export const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

function getAutoCompactThreshold(model) {
  const effectiveContextWindow = getEffectiveContextWindowSize(model)
  const autocompactThreshold = effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS
  // CLAUDE_AUTOCOMPACT_PCT_OVERRIDE 以有效窗口的百分比覆盖
}
```
所以对 200K 模型：有效 ≈ 180K，自动压缩阈值 ≈ **167K tokens**。注释与文档都描述为有效窗口的约 **93%**。

### 触发判断 —— `shouldAutoCompact()`
按顺序检查：
1. **递归防护**：`querySource === 'session_memory' || 'compact'` → return false（绝不压缩压缩器）。
2. `isAutoCompactEnabled()` —— 若 `DISABLE_COMPACT`/`DISABLE_AUTO_COMPACT` 或 `userConfig.autoCompactEnabled=false` 则 false。
3. `feature('REACTIVE_COMPACT')` / context-collapse 模式 → 抑制主动自动压缩（让 API 413 处理）。
4. **计数**：`tokenCountWithEstimation(messages) - snipTokensFreed`，判断 `tokenUsage >= getAutoCompactThreshold(model)`。

### 复查节奏
它**不是**在每个 token 上重算；自动压缩在每次 query 循环迭代时重新评估，并用 `turnCounter`/`turnId`/`compacted` 在 `AutoCompactTrackingState` 里跟踪。

### 熔断断路器（对你的 agent 很重要）
连续 `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` 次失败后，**本会话剩余时间跳过自动压缩**。代码注释："BQ 2026-03-10: 1279 个会话有 50+ 连续失败（最多 3272 次）在单会话，全球每天浪费约 250K API 调用。"

---

## Topic 2 — `/compact` 命令与自动压缩机制

### 摘要落到哪：是 user 消息，不是 system prompt
泄漏 `compact.ts` 里 `buildPostCompactMessages()`：
```
[boundaryMarker, ...summaryMessages, ...messagesToKeep, ...attachments, ...hookResults]
```
摘要是 **`user` 消息**，带 `isCompactSummary: true`、`isVisibleInTranscriptOnly: true`，由 `getCompactUserSummaryMessage()` 生成。包装文本（`prompt.ts` 已核实）：
```
"This session is being continued from a previous conversation that ran out of context.
The summary below covers the earlier portion of the conversation.
${formattedSummary}"
```
加上（自动压缩路径，`suppressFollowUpQuestions=true`）：
> "Continue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary... Pick up the last task as if the break never happened."

**所以压缩不把摘要搬进 system prompt**——它是一个合成的 user turn。（Anthropic *platform* 的 compaction API 用一个独立的 `compaction` content block，那是另一个服务端机制。）

### 是否保留工具结果？
Micro-compaction（Topic 3）以修改形式保留工具结果。但**完整压缩会把它们总结掉**——压缩 prompt（泄漏 `prompt.ts`）明确指示"列出所有非工具结果的 user 消息"（第 6 节），工具输出只在被"Files and Code Sections"（第 3 节）捕获时以摘要形式存活。所以完整压缩**不**逐字携带工具结果，只有摘要里的表示存活。

### "最近消息"保留缓冲
两个机制，均已核实：
1. **压缩后文件恢复**（泄漏 `compact.ts`）：
   ```
   POST_COMPACT_MAX_FILES_TO_RESTORE = 5
   POST_COMPACT_TOKEN_BUDGET        = 50_000
   POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
   POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
   POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
   ```
   压缩后重新读取**最多 5 个最近修改/读取的文件**（预算 50K，5K/文件），并重注入被调用的 skills（25K，5K/skill）。官方[context-window 文档](https://code.claude.com/docs/en/context-window)确认："re-reads up to five, most recently modified first." 文件 >5,000 tokens 以路径引用回来（`Referenced file`）。
2. **Session-memory 压缩保留**（泄漏 `sessionMemoryCompact.ts`）——这是明确的"keep recent"缓冲：
   ```
   DEFAULT_SM_COMPACT_CONFIG = { minTokens: 10_000, minTextBlockMessages: 5, maxTokens: 40_000 }
   ```
   从最后一个被总结的消息开始**向前**扩展，直到两个最小值都满足（≥10K token 且 ≥5 条含文本消息），上限 40K。`adjustIndexToPreserveAPIInvariants()` 会拉入孤立的 `tool_use` 和同 `message.id` 的 thinking block，避免 API 拒绝。所以"最近消息"尾部量级约 **10K–40K tokens**。

### "带重点压缩" + 自定义指令
`/compact [instructions]` 传 `customInstructions` → `getCompactPrompt()`。自定义指令**追加**到默认 prompt（"Additional Instructions:"）——注意在 *platform* compaction API 里 `instructions` **替换**默认（行为不同）。

### prompt 过长恢复（CC-1180）
如果压缩请求本身超 API 限制，`truncateHeadForPTLRetry()` 丢弃最旧的 API-round 分组（`groupMessagesByApiRound`），最多 `MAX_PTL_RETRIES = 3`，回退到丢弃 20% 分组。

### 压缩后什么存活（官方表）
| 机制 | 压缩后 |
|---|---|
| System prompt + 输出风格 | 不变（非消息历史） |
| 项目根 CLAUDE.md + 无作用域规则 | 从磁盘重新注入 |
| 自动记忆 | 从磁盘重新注入 |
| plan mode 的计划 | 从磁盘重新注入 |
| 路径作用域规则 / 嵌套 CLAUDE.md | 按 Claude 读匹配文件时重载 |
| 已读/已编辑文件 | 重读最多 5 个，最近修改优先 |
| 被调用的 skill 正文 | 重注入，5,000/skill，25,000 总，最旧先丢 |
| Hook 添加的上下文 | 与其余一起被总结 |
| 匹配 `compact` source 的 SessionStart hooks | 重跑 |

---

## Topic 3 — 工具结果 / 工具输出处理，"always cache"

这是泄漏源码里最丰富的一块，两套独立系统：

### (A) 大工具结果**持久化到磁盘** —— `toolResultStorage.ts`
常量（泄漏 `src/constants/toolLimits.ts`）：
```
DEFAULT_MAX_RESULT_SIZE_CHARS   = 50_000
MAX_TOOL_RESULT_TOKENS          = 100_000
BYTES_PER_TOKEN                 = 4
MAX_TOOL_RESULT_BYTES           = 100_000 * 4 = 400_000   // ~400KB
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000  // 单 turn user 消息聚合
PREVIEW_SIZE_BYTES              = 2000
```
行为：
- 超阈值工具结果**写盘**到 `~/.claude/projects/<project>/<session>/tool-results/<toolUseId>.txt|json`，模型只看 preview：
  ```
  <persisted-output>
  Output too large (X). Full output saved to: <path>
  Preview (first 2,000): ...
  ... </persisted-output>
  ```
- 单工具阈值 = `Math.min(declaredMaxResultSizeChars, 50_000)`，除非 GrowthBook override `tengu_satin_quoll` 提供值。
- **`maxResultSizeChars: Infinity` 的工具（Read）是硬退出**——永不持久化。注释："Read 通过 maxTokens 自限；把它的输出写到模型再用 Read 读回来的文件是循环的。"（这就是人们说的 Read 相关的"always cache"行为。）
- 状态按 `tool_use_id` 用 `seenIds` + `replacements` 冻结，使同一 preview 串每轮**逐字节一致**地重新应用（保持 prompt cache 前缀）。
- **聚合预算**：如果单 turn 并行工具结果超过 `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000`，**最大的新结果**被持久化/替换直到低于预算。`enforceToolResultBudget()`。消息（turn）独立评估。

### (B) **Micro-compaction** —— `microCompact.ts`
`COMPACTABLE_TOOLS` 集合（只有这些工具结果才做截断）：
```
FILE_READ, SHELL_TOOL_NAMES (Bash/PowerShell), GREP, GLOB, WEB_SEARCH, WEB_FETCH, FILE_EDIT, FILE_WRITE
```
**MCP 工具、Agent 工具、自定义工具不在集合里**——它们的结果一直保留到完全自动压缩。

两条路径：
1. **基于时间（冷缓存）**：`evaluateTimeBasedTrigger()` 在"距上一条主循环 assistant 消息的时间差超过阈值"时触发。配置（`timeBasedMCConfig.ts`）：
   ```
   TIME_BASED_MC_CONFIG_DEFAULTS = { enabled: false, gapThresholdMinutes: 60, keepRecent: 5 }
   ```
   触发时保留**最后 `keepRecent`（5）**个可压缩工具结果，其余替换为 `"[Old tool result content cleared]"`。注释："60 是安全选择：server 的 1h 缓存 TTL 保证已过期。"
2. **基于缓存（热缓存，仅 ant）**：用 `cache_edits` API 从**服务端缓存副本**里移除工具结果，而**不**使前缀失效。它**不**碰本地 `messages`；排队 `pendingCacheEdits` 在 API 层消费。

每个结果的 token 估算：文本 = `roughTokenCountEstimation()`；图片/文档 = **固定 2000 tokens**（`IMAGE_MAX_TOKEN_SIZE`）。

---

## Topic 4 — Token 计数与上下文窗口估算

**结论：默认用启发式，真实 API token 计数作为精化。** 两者都在 `src/utils/tokens.ts` + `src/services/tokenEstimation.ts`。

### 启发式（chars/4）
```
function roughTokenCountEstimation(content, bytesPerToken = 4) {
  return Math.round(content.length / bytesPerToken)
}
```
- **`bytesPerToken` 默认 = 4**（即 chars/4）。
- **JSON/JSONL/JSONC 用 2**（`bytesPerTokenForFileType`），因为密集 JSON 有很多单字符 token。
- 图片/文档 = **固定 2000 tokens**。
- `microCompact.ts` 的 `estimateMessageTokens()` 用 **4/3** 填充总量：`Math.ceil(totalTokens * (4/3))` 以偏保守。

### 真实 API 计数
- `countTokensWithAPI()` / `countMessagesTokensWithAPI()` 调 **`count_tokens` 端点**，带 `max_tokens: 2048`，有 thinking block 时带 `thinking.budget_tokens: 1024`。Bedrock 用 `CountTokensCommand`。某些路径回退到 Haiku。
- `tokenCountWithEstimation(messages)` 是**阈值的 canonical 函数**。它取最后一次 API 响应的 usage（`input + cache_creation + cache_read + output`）并加上 `roughTokenCountEstimationForMessages()` 对后续消息的估算。它回退越过同 `message.id` 的 sibling 记录，避免并行工具结果被低估。

### 系统 prompt + 工具 + 历史的预算
**`/context`** 命令（`analyzeContext.ts`，1383 行）按类别分解窗口：system prompt、memory 文件、内置工具（常加载 vs defer）、MCP 工具、skills、消息（tool calls vs results vs text）、autocompact buffer 预留。与 `getEffectiveContextWindowSize()` 比较。

---

## Topic 5 — 记忆：CLAUDE.md、auto memory、`#` 添加记忆、重注入

### CLAUDE.md 加载顺序（低→高优先级，后者更高）
```
1. Managed   /etc/claude-code/CLAUDE.md（或 OS 等价）
2. User      ~/.claude/CLAUDE.md
3. Project   ./CLAUDE.md, ./.claude/CLAUDE.md
4. Local     ./CLAUDE.local.md（gitignored）
5. AutoMem   ~/.claude/memory/MEMORY.md
6. TeamMem   （feature-gated）
```
从 CWD 向上到 root 走目录，root→CWD 处理，所以 CWD 本地文件最后加载（最高优先级）。支持 `@path` 导入（`.md`，最大深度 5），以及 `.claude/rules/*.md` 里通过 YAML `paths:` frontmatter 的路径作用域规则。

### CLAUDE.md 如何注入
它在 **system prompt 之后作为一条 user 消息**交付，不是 system prompt 的一部分。渲染串（泄漏 `claudemd.ts`）：
```
"Codebase and user instructions are shown below. Be sure to adhere to these instructions.
IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written."
```

### 自动记忆
- 存储：`~/.claude/projects/<project>/memory/`，带 `MEMORY.md` 索引。
- **加载 `MEMORY.md` 的前 200 行或 25KB，先到为准。** 主题文件（`user_role.md` 等）**不**在启动时加载——Claude 用文件工具按需读。
- 类型：`user`、`feedback`、`project`、`reference`（存在 frontmatter `type`）。
- `CLAUDE.md` 完整加载最大 4 MiB，更大则跳过。目标 <200 行。

### `#` / "add to memory" 流程
你让 Claude 记住某事（"always use pnpm, not npm"）→ Claude 存进 **auto memory**。要强制写进 CLAUDE.md 文本，则说"add this to CLAUDE.md"或用 `/memory` / 编辑文件。UI 里的 `#` 简写就是"记住这个"路径。

### 压缩后重注入
通过从磁盘重注入（项目根 / 无作用域 / 自动记忆）存活。路径作用域 + 嵌套文件在读到匹配文件时懒加载。所以 memory 跨会话持久，但它是**上下文而非强制**——硬强制用 PreToolUse hooks。

---

## Topic 6 — 上下文窗口 flags 与用户可见限制

### 命令 / flags（官方 [model-config](https://code.claude.com/docs/en/model-config) + [context-window](https://code.claude.com/docs/en/context-window)）
- **`/context`** —— 按类别实时分解（system prompt、tools、memory、skills、messages、free space）。
- **`/autocompact 500k`**（及 `--autocompact`、`CLAUDE_CODE_AUTO_COMPACT_WINDOW`）—— 设置自动压缩窗口。接受 100K–1M 纯数字、`k`/`M` 后缀、或裸 100–1000（=千）。`/autocompact auto` 恢复每模型调优值。**env var 优先级高于命令/flag/设置。**
- **`CLAUDE_CODE_AUTO_COMPACT_WINDOW`** —— 缩小有效窗口（只减不增，不能超过模型真实窗口）。
- **`CLAUDE_CODE_MAX_CONTEXT_TOKENS`** —— 为 gateway/自定义模型 id 声明窗口。
- **`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`** —— 有效窗口百分比触发，上限 `Math.min(percentThreshold, autoCompactThreshold)`。
- **`CLAUDE_CODE_DISABLE_1M_CONTEXT=1`** —— 强制 200K。
- **`DISABLE_COMPACT`** / **`DISABLE_AUTO_COMPACT`** —— 完全禁用。
- **`CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`** —— 覆盖阻塞上限（`effectiveWindow - 3_000`）。

### 用户可见的"Context left until auto-compact"
`calculateTokenWarningState()` 计算四档阈值的 `% left`（泄漏 `autoCompact.ts`）：
```
percentLeft = (threshold - tokenUsage)/threshold * 100
isAboveWarningThreshold:      threshold - 20_000
isAboveErrorThreshold:        threshold - 20_000
isAboveAutoCompactThreshold:  threshold - 13_000（即 == autoCompactThreshold）
isAtBlockingLimit:            threshold - 3_000  （需要手动 compact）
```
真实 `/context` 输出示例（GitHub issue #43989）：
```
Opus 4.6 (1M context) · 23.8k/1m tokens (2%)
  System prompt: 6.3k   System tools: 8.6k   Memory files: 8.3k
  Skills: 493   Messages: 8   Free space: 955.2k (95.5%)
  Autocompact buffer: 21k tokens (2.1%)
```

### 真实 bug 展示窗口如何被封顶
GitHub issue [#43989](https://github.com/anthropics/claude-code/issues/43989)：**v2.1.92 悄悄把 Opus 4.6 的 autocompact 窗口封顶在 ~400K**（v2.1.91 是 ~1M）。`/context` 显示 `25.7k/400k`，会话在 ~400K 时压缩。回滚到 v2.1.91 恢复 1M。这活生生说明自动压缩窗口是**用户可见、随版本变**的数字——也警告泄漏常量会在版本间变化。

---

## Topic 7 — Prompt caching（`cache_control`）与什么使缓存失效

### 如何组织内容以最大化缓存命中
请求排序让**少变内容在前**（前缀匹配是精确的）：
```
1. System prompt    → 工具定义变化或 Claude Code 升级时变
2. 项目上下文        → CLAUDE.md、auto memory、无作用域规则（session 开始 /clear /compact 时变）
3. 对话            → messages、responses、tool results（每 turn 都变）
```
System prompt 在 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 处分：静态段用 `scope: 'global'`（跨组织可缓存），动态段用 `scope: 'session'`。

### 什么破坏缓存（KV 失效）—— 官方清单
- **换模型**（`/model`）—— 每个模型有自己的缓存。
- **换 effort 级别**（`/effort`）—— 缓存按 effort 也 keyed。
- **Fast mode 开**—— 加一个属于 cache key 的 header（一次全 miss）。
- **MCP server 连接/断开**，当工具加载进前缀（deferred 工具不失效）。
- **启用/禁用提供前缀加载 MCP 工具的插件**。
- **拒绝一个完整工具**（裸名、`Bash(*)`、`"*"`）—— 从 system prompt 移除内置工具定义。作用域规则如 `Bash(rm *)` 不改前缀。
- **压缩**—— 使对话层失效（更短新历史）。
- **升级 Claude Code** —— 改变 system prompt/工具定义，从头重建。

### 什么不破坏缓存（追加或不碰请求）
- 编辑 repo 文件（追加 `<system-reminder>`；早期读取不变）。
- 会话中途编辑 CLAUDE.md（启动时加载一次；重启/clear/compact 前不生效）。
- 会话中途改输出风格（同上）。
- 改权限模式（除 `opusplan`，它换模型）。
- 调用 skills/命令（作为 user 消息注入，后缀）。
- `/recap`（作为命令输出追加，非替换）。
- `/rewind`（截断回已缓存的 prefix）。

### TTL（已核实，随版本）
两个桶：
| 请求桶 | Claude sub，plan 内 | API key / credits / cloud |
|---|---|---|
| 主对话 | 1 小时 | 5 分钟 |
| 其它（subagents、forks、compaction、titles） | 5 min（服务端控制的 helper 除外=1h） | 5 min |
控制：`promptCacheTtl`/`CLAUDE_CODE_PROMPT_CACHE_TTL`（主）、`subagentPromptCacheTtl`/`CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`。优先级：`FORCE_PROMPT_CACHING_5M=1` → env → setting → subagent `cacheTtl` → `ENABLE_PROMPT_CACHING_1H=1` → 默认。需 v2.1.242+。

### 缓存作用域
缓存前缀内嵌 CWD、platform、shell、OS、auto-memory 路径——所以实际上是**一台机器 + 一个目录**的作用域。同目录两个并行会话共享缓存；不同目录/worktrees 则 miss。

### 缓存指标
`cache_creation_input_tokens`（写）vs `cache_read_input_tokens`（读，约输入费率 10%）。高 read:creation 比 = 健康。

---

## Topic 8 — Micro-compaction 与 context rot（它填充小部分窗口）

### Micro-compaction 作为 *缓存策略 + 持久化格式*
设计（来自 `decodeclaude.com/compaction-deep-dive/` 对发布 bundle 的逆向，并经泄漏 `microCompact.ts` 确认）：
- **热尾部**：一小段较近的工具结果窗口保持完全可见（内联）。显式旋钮 `keepRecent: 5` + 60 分钟 gap 阈值（时间路径）。
- **冷存储**：更旧的全部按路径引用（`[Old tool result content cleared]`，或 `<persisted-output>...saved to: <path>`）。
- 只有 `COMPACTABLE_TOOLS` 可用；MCP/Agent/自定义工具保留到完全压缩。

所以"micro-compaction 填充小部分窗口"更准确地说是 **`keepRecent` 保留缓冲 + `maxTokens`/`minTokens` session-memory 上限**（见 Topic 2 & 3）。泄漏的 `sessionMemoryCompact.ts` 是最有力的证据表明"保留一个小近期尾部，把它之前的全总结掉"的设计。

### Context rot —— 真实且已文档化，带数字
Anthropic 官方[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)（2025-09）：
> "对 needle-in-a-haystack 风格基准的研究发现了 context rot：随着上下文中 token 数增加，模型从该上下文准确召回信息的能力下降。"

它把上下文视为有限资源，边际收益递减，一个"注意力预算"（n² 两两注意力）。建议找到"最小可能的高信号 token 集。"

**社区测得的拐点**（非官方，但一致，标注为此）：
- [vincentvandeth.nl blog](https://vincentvandeth.nl/blog/context-rot-claude-code-automatic-rotation)：*"发生在上下文窗口填满约 65% 之后。"* 他在 ~60–65% 时轮换，因为"Claude Code 内置自动压缩约 80% 才触发，要留余量。"
- [Reddit r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1r4tu3n/stop_claude_code_from_going_dumb_autocompact/)：*"Claude Code 在约 50% 上下文用量后就明显变差。默认自动压缩到 ~95% 才触发——太晚了。*"`
- 引用"Lost in the Middle"（Liu et al., TACL 2024）——长上下文中部检索退化 15–47%。

**精确性提示**："65%" / "50%" / "80%" 社区数字**不是**内部 167K/200K（≈83%）缓冲。Anthropic 自身触发在有效窗口约 93%，但按社区质量退化早就开始。**没有官方 Anthropic 数字**说"context rot 在 X% 开始"；只有内部自动压缩缓冲可从源码核实。

---

## 给 agent_lite 的具体"take-away"常量

若想镜像 Claude Code 设计，可核实的数字是：

| Concern | 值（Claude Code） | 来源 |
|---|---|---|
| 上下文窗口默认 | 200,000 tokens（`MODEL_CONTEXT_WINDOW_DEFAULT`） | 泄漏 `context.ts` |
| 1M 窗口 | `[1m]` 后缀 / Sonnet 5 / Opus 4.6+ | 官方 docs + 泄漏 |
| 输出/压缩预留 | `min(maxOutput, 20,000)`；摘要 p99.99 = 17,387 | 泄漏 `autoCompact.ts` |
| 自动压缩缓冲 | 13,000 tokens（有效窗口约 93%） | 泄漏 `autoCompact.ts` |
| Warning / error 阈值缓冲 | 20,000 | 泄漏 |
| Blocking（需手动压缩）缓冲 | 3,000 | 泄漏 |
| 自动压缩最大连续失败 | 3（熔断） | 泄漏 |
| 压缩后恢复文件 | 最多 5，50K 预算，5K/文件 | 泄漏 `compact.ts` |
| Skill 重注入预算 | 5K/skill，25K 总 | 泄漏 |
| Session-memory "keep recent" | min 10K token，min 5 文本消息，max 40K | 泄漏 `sessionMemoryCompact.ts` |
| Micro-compact keepRecent | 5，gap 阈值 60 min（`enabled:false` 默认） | 泄漏 `timeBasedMCConfig.ts` |
| 工具结果持久化阈值 | 50K 字符默认（单工具 min），100K token/400KB 最大，200K 字符/turn 聚合 | 泄漏 `toolLimits.ts` |
| Bytes-per-token 启发式 | 默认 4，JSON=2，image=2000，pad 4/3 | 泄漏 `tokens.ts`/`tokenEstimation.ts` |
| MEMORY.md 加载上限 | 200 行或 25KB | 官方 memory docs |
| CLAUDE.md 上限 | 4 MiB（完整加载），推荐 <200 行 | 官方 memory docs |
| 缓存 TTL 默认 | 1h（sub 主对话）/ 5m 其它 | 官方 prompt-caching docs |

**对本模块的建议**：实现**三层**设计（逐 turn 的 micro-compact → 带"keep recent"尾部的 session-memory compact → 完整带结构化 9 节摘要 prompt + 注入前剥离 `<analysis>` scratchpad 的 LLM compact）。保留熔断器和 prompt 过长重试（丢最旧 API-round 组）。**不要**硬编码 Claude Code 的精确缓冲数字当作真理——它们随版本、且取自泄漏；应针对你自己的模型/tokenizer 重建。

---

## 参考来源

- 官方 docs：https://code.claude.com/docs/en/context-window · https://code.claude.com/docs/en/prompt-caching · https://code.claude.com/docs/en/memory · https://code.claude.com/docs/en/model-config · https://platform.claude.com/docs/en/build-with-claude/compaction · https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools · https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- 泄漏源码（镜像）：`alex000kim/claude-code` —— `src/services/compact/autoCompact.ts`、`microCompact.ts`、`sessionMemoryCompact.ts`、`timeBasedMCConfig.ts`、`prompt.ts`、`compact.ts`、`src/utils/tokens.ts`、`src/utils/toolResultStorage.ts`、`src/constants/toolLimits.ts`、`src/utils/context.ts`；`src/services/tokenEstimation.ts`
- 逆向分析：`openedclaude/claude-reviews-claude`（`architecture/10-context-assembly.md`、`11-compact-system.md`、`09-session-persistence.md`）· https://decodeclaude.com/compaction-deep-dive/ · https://www.straiker.ai/blog/claude-code-source-leak-with-great-agency-comes-great-responsibility
- 社区/报告：https://github.com/anthropics/claude-code/issues/43989 · https://vincentvandeth.nl/blog/context-rot-claude-code-automatic-rotation · https://www.reddit.com/r/ClaudeAI/comments/1r4tu3n/stop_claude_code_from_going_dumb_autocompact/

**显式说明的不确定性**：(1) 泄漏常量来自 2026-03-31 的 2.1.88 泄漏，可能不匹配当前构建（issue #43989 证明这些值会变）。 (2) "context rot 在 50–65% 开始"是社区测量，非 Anthropic 官方。 (3) Claude Code 的"cached microcompact"（`cache_edits`）在泄漏里是 ant-only/feature-gated，可能不存在于外部/API key 用户。
