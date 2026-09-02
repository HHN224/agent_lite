# 其它成熟 Coding Agent — LLM 上下文管理调研

> 调研目的：为 agent_lite 的上下文管理模块提供参考。
> 来源标注：**[源码]** = 直接读了实际仓库源码（路径 + GitHub URL）；**[文档]** = 官方文档/博客/changelog；**[博客]** = 第三方博客/社区（可能是营销）。
> **开源与闭源**：Aider、pi-agent、OpenHands、Gemini CLI、Cline、Roo、Continue、Goose、SWE-agent 均已从源码核实；**Cursor、Copilot coding agent、Amp、Devin 是闭源**，仅有文档/博客。

---

## 1. Aider（paul-gauthier/aider）

**(a) 触发。** 自动（不是 `/compact` 命令）。在 `base_coder.py`，`summarize_start()` 在 `done_messages` 超过 `max_chat_history_tokens` 时触发，后台线程。**[源码]** `aider/coders/base_coder.py`, `aider/history.py`。

**(b) 机制。** `ChatSummary.summarize_real` 保留约**一半预算**的尾部逐字（`half_max_tokens = max_tokens // 2`，递归深度 ≤3），把头部总结成一个**单个 `role:user` 消息**（不把整个历史重写成 XML）。`summarize_all` 把 `system=summarize` + 拼接的 `USER/ASSISTANT` 记录发给弱模型。**[源码]** `aider/history.py`。

**(c) 最近缓冲。** 尾部保留约 `max_tokens/2` token。**[源码]** `history.py`。

**(d) Token 计数。** 通过 litellm 精确 (`token_count` → `litellm.token_counter`, `tokenizer` → `litellm.encode`)。图片成本 = `num_tiles*170 + 85`。**[源码]** `aider/models.py`。

**(e) 工具输出。** 渲染为聊天文本；`/run` 输出可选地以显式 token 提示 + 用户确认添加。自动提交经**弱模型**生成简洁 git 消息（`commit_message_models()` = `[weak_model, main_model]`）。**[源码]** `base_coder.py`。

**(f) 独特点。** **Repo-map**：基于文件的图做 networkx `pagerank`，预算用二分搜索拟合。每模型预算 `get_repo_map_tokens()` = `max_input_tokens/8` 夹在 `[1024,4096]`；无文件在聊天时扩展到 `min(map_tokens*8, context_window−4096)`。聊天历史上限 `max_chat_history_tokens = min(max(max_input_tokens/16,1024),8192)`。上下文窗口耗尽**上报**（fudge=0.7）但**不自动修复**。**[源码]** `aider/repomap.py`, `aider/models.py`。默认 `map_tokens=1024`。**[文档]** https://aider.chat/docs/repomap.html。

---

## 2. Gemini CLI（google-gemini/gemini-cli）

两套独立系统。压缩在 `@google/gemini-cli-core`（`packages/core/src/`）。

**(a) 触发。** 系统 A —— `/compress` 自动路径：`chatCompressionService.ts`，`DEFAULT_COMPRESSION_TOKEN_THRESHOLD = 0.5`，所以在 `tokenLimit(model)` 的 **50%**（默认 `DEFAULT_TOKEN_LIMIT = 1_048_576` → 约 524k token；Gemma 4 = 256k）自动压缩。手动 `/compress` 强制绕过。系统 B —— 自动上下文管理器（`contextManager.ts`，默认**关闭**）：`retainedTokens: 65000`、`maxTokens: 150000`、`coalescingThresholdTokens: 5000`。**[源码]** `packages/core/src/context/chatCompressionService.ts`、`.../core/tokenLimits.ts`、`.../context/config/profiles.ts`。

**(b) 机制。** 系统 A 把较旧 **70%** 总结成一个 **`<conversation_snapshot>` XML 合成 user 消息**（框定为 agent 的"唯一对过去的记忆"，带显式 **prompt-injection 防御**），保留近期 **30%** 逐字，并跑一个**两阶段自校验"探针"**（生成 → 批判 → 终稿）。用专门总结模型（`chat-compression-3-pro` 等）。**[源码]** `chatCompressionService.ts`、`prompts/snippets.ts`。

**(c) 最近缓冲。** `COMPRESSION_PRESERVE_THRESHOLD = 0.3`（按字符权重而非消息数保留最后 30%）；文件级 `RECENT_TURNS_PROTECTED = 2`。系统 B：`retainedTokens: 65000`。**[源码]**。

**(d) Token 计数。** 本地启发式而非精确 tokenizer：`ASCII_TOKENS_PER_CHAR=0.33`、`NON_ASCII_TOKENS_PER_CHAR=1.5`、`DEFAULT_CHARS_PER_TOKEN=4`，图片 3000，PDF 25800。仅在**有媒体**时用精确 GenAI `countTokens` API。**[源码]** `packages/core/src/utils/tokenCalculation.ts`。

**(e) 工具输出。** **四层**：遮蔽→磁盘（保护最新 50k，只在 >30k 可剪枝时触发；输出卸载到 `<project-temp>/tool-outputs/...`，替换为 `<tool_output_masked>` 片段）；蒸馏（最大 10k token，`read_file` 豁免）；反向预算截断（预算超 50k 后更旧结果截到最后 30 行）；文件级 `FULL/PARTIAL/SUMMARY/EXCLUDED`。若 `functionCall`→`functionResponse` 邻接被破坏，结构保护回退到未压缩历史。**[源码]** `toolOutputMaskingService.ts`、`toolDistillationService.ts`、`contextCompressionService.ts`。

**(f) 独特点。** XML 快照作为唯一记忆；两阶段自校验；专门总结模型路由；工具输出磁盘卸载；"反向 token 预算"（最新逐字保留，更旧截断）。`/compress`（别名 `summarize`,`compact`）。**[源码]**。

---

## 3. pi-agent（现 `earendil-works/pi`，TypeScript）

**仓库修正**：`github.com/badlogic/pi-mono` 重定向到 `earendil-works/pi`。压缩在 `packages/agent/src/harness/compaction/`（不是旧的 `packages/coding-agent/src/core/compaction/`）。它是 **TypeScript** —— 不是 Rust，**没有 `pi-tokenizer` crate**。

**(a) 触发。** **[源码]** `compaction.ts`：`shouldCompact()` 返回 `contextTokens > contextWindow - reserveTokens`；`DEFAULT_COMPACTION_SETTINGS = { enabled:true, reserveTokens:16384, keepRecentTokens:20000 }`。无 turn 限制。手动 `/compact [instructions]`。**[文档]** https://pi.dev/docs/latest/compaction。

**(b) 机制。** **Append-only session + 重建上下文**（从不改写记录）。存储的 session 是完整 JSONL；模型可见上下文重建为：system prompt → 压缩摘要消息 → 从 `firstKeptEntryId` 起的保留尾部。总结用严格的结构化检查点（`## Goal`、`## Constraints`、`## Progress`、`## Key Decisions`、`## Next Steps`、`## Critical Context`），带禁止延续的 system prompt。重复压缩从上一个压缩的保留边界开始。**[源码]** `compaction.ts`、`session/context.ts`、`messages.ts`。

**(c) 最近缓冲。** `keepRecentTokens = 20000` token 逐字保留（`retainedTail`）。切点吸附到有效边界（user/assistant/bashExecution/custom/branch_summary，**绝不** toolResult）。**[源码]** `findValidCutPoints`/`findCutPoint`。

**(d) Token 计数。** **chars/4 混合**：`estimateTokens` 用 `ceil(char/4)` 每条消息，另加 `ESTIMATED_IMAGE_CHARS=4800`（约 1200 token）每图；`estimateContextTokens` 锚到最后一条 assistant 消息的 **provider `usage`**（`totalTokens` 或 input+output+cacheRead+cacheWrite），只对尾部消息用 chars/4。**[源码]** `compaction.ts`。

**(e) 工具输出。** 实时上下文里是原生 `toolCall`/`toolResult` 消息。总结时用 `serializeConversation` 转成纯文本并**截断工具结果到 `TOOL_RESULT_MAX_CHARS = 2000`**。保留尾部保留完整结果。**[源码]** `compaction/utils.ts`。

**(f) 独特点。** Append-only session；`/compact`；`/tree` 分支总结；**累计文件追踪**（`<read-files>`/`<modified-files>` 跨压缩累积）；split-turn 双重摘要；skill 渐进加载；扩展钩子 `session_before_compact`。**[源码]** + **[文档]**。

---

## 4. SWE-agent / SWE-bench（princeton-nlp/SWE-agent）

**(a) 触发。** **无 token 比例自动压缩**。Tokenizer 精确的上下文溢出抛 `ContextWindowExceededError` → **退出**/自动提交。护栏（成本 `per_instance_cost_limit=3.0`、调用/超时限制）停止运行，而非压缩上下文。**[源码]** `sweagent/agent/models.py`、`agents.py`。

**(b) 机制。** **确定性过滤器链** `history_processors`（`sweagent/agent/history_processors.py`），从不用 LLM 总结：`LastNObservations`（省略 → `"Old environment output: (N lines omitted)"`）、`TagToolCallObservations`、`ClosedWindowHistoryProcessor`（折叠过期文件窗口）、`CacheControlHistoryProcessor`（标记最后 `n` 条消息 `cache_control: ephemeral`）、`RemoveRegex`。默认配置用 `cache_control`（最后 2 条消息）。**[源码]** + **[文档]** `docs/reference/history_processor_config.md`。

**(c) 最近缓冲。** 按 processor 而非全局：`LastNObservations.n`（论文=5，`polling` 时保留 n..n+polling）、`RemoveRegex.keep_last`。默认为"保留整条轨迹"。**[源码]**。

**(d) Token 计数。** **精确 tokenizer** 经 `litellm.utils.token_counter`，`max_input_tokens` 来自 `model_cost`。**[源码]** `models.py`。

**(e) 工具输出。** ACI 模板 `Observation: {{observation}}`；硬**写入时**字符上限 `max_observation_length = 100_000`（默认），附指令要求少产出；空 → `"no output"` 模板。**[源码]** `agents.py`。

**(f) 独特点。** 可组合历史处理器；prompt-caching 原语；`windowed` 工具 + closed-window 折叠；成本/调用"退出"护栏；重放 `.traj`。**[源码]**。

---

## 5. OpenHands（All-Hands-AI → 现 `OpenHands/software-agent-sdk`）

**路径注意**：condenser 移到 `openhands-sdk/openhands/sdk/context/condenser/`。直接核实的源码。

**(a) 触发。** 多信号 `condensation_requirement()` → `Reason.EVENTS`（`len(view) > max_size`）、`Reason.TOKENS`（超过 condenser `max_tokens` / agent LLM `effective_max_input_tokens` 中更严者）、`Reason.REQUEST`。映射为 **HARD**（TOKENS、REQUEST）vs **SOFT**（仅 EVENTS）。类默认 `max_size=240`，但发布 `default_condenser()` 用 `_DEFAULT_MAX_SIZE=80`、`_DEFAULT_KEEP_FIRST=4`、`minimum_progress=0.1`。**[源码]** `llm_summarizing_condenser.py`、`base.py`。

**(b) 机制。** **总结中间，保留头部+尾部逐字**（滚动窗口）。`_get_forgotten_events` 按 reason 计算目标大小（`REQUEST→len//2`、`EVENTS→max_size//2`、`TOKENS→get_suffix_length_for_token_reduction`），取最严者，对齐 `manipulation_indices`。被遗忘的中间交给**独立的 `self.llm`** 经 `summarizing_prompt.j2` → 一个 `Condensation` 事件（append-only）。`CondensationSummaryEvent.to_llm_message()` 把摘要作为 **user** role 消息发出。`hard_context_reset` 回退总结所有事件（offset 0），把事件字符串缩小 0.8 倍 ×5 重试。**[源码]**。

**(c) 最近缓冲。** `keep_first` 事件永不被总结（类默认 2；发布 4）；尾部 = `target_size - keep_first - 1` 事件逐字保留。**[源码]** + **[文档]** https://docs.openhands.dev/sdk/arch/condenser。

**(d) Token 计数。** **精确、LLM 辅助** 经 litellm（`get_total_token_count` → `llm.get_token_count`）。**[源码]** `condenser/utils.py`。

**(e) 工具输出。** Observations 是 `LLMConvertibleEvent`；`Condensation.forgotten_event_ids` 是集合，所以整个工具 observation 要么逐字保留（尾部）、要么折叠进摘要。**[源码]**。

**(f) 独特点。** **Hard/Soft condensing 需求**；**`PipelineCondenser`**（组合 remove→summarize→truncate）；`NoOpCondenser`；manipulation-index 对齐使工具循环不会被切半；优雅的 `NoCondensationAvailableException` + hard reset。**最可复用的架构。** **[源码]**。

---

## 6. Continue.dev（continuedev/continue）

**(a) 触发。** 无比例触发——**每次请求的 token 预算**在 `compileChatMessages`。`DEFAULT_CONTEXT_LENGTH=32768`、`DEFAULT_MAX_TOKENS=4096`、`DEFAULT_PRUNING_LENGTH=128000`。缓冲 `min(1000, contextLength*0.02)`、`MIN_RESPONSE_TOKENS=1000`。system+最后 user/tool+tools 放不下就抛异常。**[源码]** `core/llm/countTokens.ts`、`constants.ts`。

**(b) 机制。** **纯截断（丢最旧），从不总结** —— `historyWithTokens.shift()` 从头部移除直到预算内，跳过孤立的头部 tool 消息。重组 `[system, ...remaining, ...toolSequence]`。**[源码]** `core/llm/countTokens.ts`。

**(c) 最近缓冲。** 整个最后 user/tool 序列总是保留；system + 完整工具 schema 总是保留（宁抛异常不丢）。无固定消息数缓冲。**[源码]**（`extractToolSequence`）。

**(d) Token 计数。** **真实 tokenizer**：`js-tiktoken`（`encodingForModel`）用于 GPT 类，否则 Llama BPE。`BASE_TOKENS=4`、工具调用 `+10`、工具输出 `+10`、图片固定 `1024`。**[源码]**。

**(e) 工具输出。** 内联 `role:tool` 消息 +10 安全；剪枝是工具调用感知的（绝不孤立工具结果）。**[源码]**。

**(f) 独特点。** 安全缓冲 + 最小输出预留；模型感知；**context providers**（`@file`、`@codebase`、`@repoMap`）与截断正交。最简单/最便宜（剪枝时无 LLM 调用）—— 适合轻量 agent，但丢失更早信息。**[源码]** `core/context/providers/`。

---

## 7. Goose（block/goose → `aaif-goose/goose`）

**(a) 触发。** `DEFAULT_COMPACTION_THRESHOLD: f64 = 0.8`（>80% 窗口压缩）；env `GOOSE_AUTO_COMPACT_THRESHOLD`；`ContextLengthExceeded` 时反应式（最多 2 次连续）。**[源码]** `crates/goose-context-management/src/lib.rs`、`crates/goose/src/context_mgmt/mod.rs`。

**(b) 机制。** **LLM 总结并重写**（非破坏）：定位最近的 user 文本消息，逐字保留，把其余总结成一个**单个 `Role::User` 消息** + 一个 assistant 延续。原消息标记 `agent_invisible` 但用户仍可见（可见性元数据重写）。**[源码]** `context_mgmt/mod.rs`、`summarize.rs`。

**(c) 最近缓冲。** 最新 user prompt 逐字保留；当前 turn 里的工具配对保护窗口（`protect_last_n` 工具调用）。**[源码]**。

**(d) Token 计数。** **带 LRU 缓存的精确 tokenizer**：`tiktoken_rs::o200k_base()`、`MAX_TOKEN_CACHE_SIZE=1024`。`tokens_per_message=4` + 工具调用 + 工具响应 + `3` 回复初始化。**[源码]** `crates/goose/src/token_counter.rs`。

**(e) 工具输出。** 总结期间渐进移除工具响应（`REMOVAL_PERCENTAGES=[0,10,20,50,100]`，从中间向外丢）；**逐工具配对总结**在后台 tokio 任务（`TOOLCALL_SUMMARIZATION_BATCH_SIZE=10`，cutoff `(3*effective_limit/20000).clamp(10,500)`）。**[源码]**。

**(f) 独特点。** Append-only 可见性重写；两层压缩（整个对话 + 细粒度工具配对）；**audience-aware 投影**；结构化摘要 schema。**[源码]**。

---

## 8. Cline（github.com/cline/cline）

**(a) 触发。** `COMPACTION_TRIGGER_RATIO = 0.9`（在可用输入预算的 90% 压缩，非用户滑块）。`DEFAULT_TARGET_RATIO = 0.7`。`DEFAULT_MAX_INPUT_TOKENS = 128_000` 回退。`CONTEXT_WINDOW_INPUT_RATIO = 0.9`。溢出恢复模式在 provider 拒绝时强制压缩。**[源码]** `sdk/packages/core/src/extensions/context/compaction-shared.ts`、`compaction.ts`。

**(b) 机制。** 两种策略 + 自定义钩子（`CoreCompactionStrategy = "basic" | "agentic"`，默认 agentic）。Agentic = LLM 总结器产出 `user` 消息带 `kind:"compaction_summary"`、`displayRole:"system"`；basic = 确定性本地折叠带"dropped-work summary"块（无 LLM）。运行在**投影**（压缩 sidecar）上，canonical 记录完整。**[源码]**。

**(c) 最近缓冲。** `DEFAULT_PRESERVE_RECENT_TOKENS = 20_000`（token 预算）；`findCutIndex` 吸附到安全边界（从不切 tool_use/tool_result）；最新 typed prompt 总是存活。Basic 保留 `PRESERVED_ASSISTANT_TEXT_COUNT = 3`。**[源码]**。

**(d) Token 计数。** **chars/3 启发式**（`CHARS_PER_TOKEN = 3`），故意多计使触发早于 provider 拒绝。**[源码]** `sdk/packages/shared/src/llms/tokens.ts`。

**(e) 工具输出。** 压缩时：序列化并截断到 `TOOL_RESULT_CHAR_LIMIT = 2000`、`FILE_CONTENT_CHAR_LIMIT = 2000`；从工具输入和编号 diff 提取文件名/编辑行范围。`FileContextTracker` 标记文件 active/stale 带读/编辑日期（带外编辑触发重载）。**[源码]**。

**(f) 独特点。** 溢出恢复确定性模式；持久压缩 sidecar 投影；预算投影策略。`/compact`（别名 `/smol`）；当前树无 `/context` 命令。**[源码]**。

---

## 9. Roo Code（github.com/RooCodeInc/Roo-Code）

**(a) 触发。** `src/core/context-management/index.ts`：`TOKEN_BUFFER_PERCENTAGE = 0.1`（10% 缓冲），当 `contextPercent >= autoCondenseContextPercent`（默认 **100%** 滑块，文档示例 80%）或 `prevContextTokens > allowedTokens`（`allowedTokens = contextWindow * 0.9 - reservedTokens`）。`MIN_CONDENSE_THRESHOLD=5`、`MAX_CONDENSE_THRESHOLD=100`。按 profile 覆盖 `[5,100]`，`-1` 继承。文档：30% 预留（20% 输出 + 10% 安全）→ 70% 可用。**[源码]** + **[文档]** https://roocodeinc.github.io/Roo-Code/features/intelligent-context-condensing/。

**(b) 机制。** **"Fresh start" LLM 总结**：把 tool_use/tool_result 转文本，总结成一个 **`role:user` 消息**带 `## Conversation Summary` + 保留的斜杠命令块。非破坏（`condenseParent` 标签；`getEffectiveApiHistory` 过滤）。回退：滑动窗口截断移除 50% 可见消息。用**同一 provider/model**（避免工具格式"翻译"）。**[源码]** `src/core/condense/index.ts`。

**(c) 最近缓冲。** 仅摘要式 fresh start（非 token 预算）；保留命令块 + 折叠文件上下文（`foldedFileContext.ts` → 函数/类签名经 tree-sitter，上限 `maxCharacters=50000`）。**[源码]**。

**(d) Token 计数。** **Provider 原生** `apiHandler.countTokens()`（如 Anthropic），回退 tiktoken；图片约 300 token。**[源码]** + **[文档]**。

**(e) 工具输出。** 工具块转文本用于摘要（payload 保留在存储）；读取文件折叠为签名（tree-sitter）使结构感知在没有完整 body 下幸存。`FileContextTracker`（fork 自 Cline）。**[源码]**。

**(f) 独特点。** 非破坏 condense + **shadow-git 检查点**（`ShadowCheckpointService.ts`、`ENABLE_AUTO_CHECKPOINTS`）使 rewind 恢复 condense 前消息；按 profile 阈值覆盖；上下文错误自动恢复（截断 25% + 重试）。**[源码]** + **[文档]**。

---

## 10. Cursor、Copilot coding agent、Amp、Devin（闭源——仅文档/博客）

**Cursor** —— 窗口"填满"时自动总结；`/summarize`。**History-as-file**：给 agent 一个可 grep 的聊天历史文件引用，用于恢复丢失细节。第三方工具输出写到文件并 `tail`（不截断；A/B 声称 MCP 工具调用时 token −46.9%）。**Self-summarization 内嵌到 Composer**（compact 约 1k token 摘要）。无公开 % 触发、最近缓冲、tokenizer。"Model-selected-on-summary" **未证实**（仅博客）。**[文档]** cursor.com/blog/dynamic-context-discovery + self-summarization；**[博客]** 对 `/summarize` 和论坛数字。

**Copilot / Copilot Coding Agent** —— **唯一有公开自动压缩比例的**：**[文档]**（Microsoft Learn）*"在上下文窗口容量约 80% 时 Copilot CLI 自动开始压缩，约 20% 缓冲，约 95% 暂停"*；`/compact`。机制：结构化摘要**替换**历史 + 保留用户指令/计划状态，创建可审计**检查点**。tokenizer/window 未文档化（社区说 128k→192k）。**注意**：广泛引用的"约 30–40% 预留输出"是 **[博客]**，有争议，别信。

**Amp** —— **设计上无自动压缩**（"Agents get drunk"；保持线程短）。手动 Handoff/Fork/Edit-Restore；第二模型蒸馏在 handoff 和 `read_thread`。具体常量：1M 上下文（968k 入 + 32k 出）、">200k token ≈ 2× 价格"、"20% of window" 提示、@ 提及文件截断到 **500 行 / 2KB 每行**（行/字节启发式，非 tokenizer）。**[文档]** ampcode.com。

**Devin** —— 自动压缩 + `/compact` + `/context`，但**无公开比例**（changelog 确认存在，多轮压缩）。`/recap`、`/handoff`。**Knowledge** 特性在对话窗口**之外**加上下文，由 Trigger Description 自动召回，repo 可固定。Shell scrollback 上限约 3500 行，中间省略；常开的 AGENTS.md 每个上限 32 KiB。触发 %、最近缓冲、tokenizer 全**未知/不可核实**。**[文档]** docs.devin.ai。

---

## 最终对比汇总表

| Agent | 触发 | 机制 | 保留近期 | Token 方法 | 工具输出处理 | 独特点 |
|---|---|---|---|---|---|---|
| **Aider** | 历史 > `max_chat_history_tokens` = min(max(max_input/16,1024),8192) [自动] | 总结头部→单条 user 消息；尾部保留约半预算 | 约 `max_tokens/2` token | 精确 litellm tokenizer | 聊天文本；弱模型自动提交 | PageRank 文件图 repo-map；预算=max_input/8 夹[1024,4096] |
| **Gemini CLI** | **50%** of tokenLimit（默认 1,048,576）；图预算 65k 保留/150k 最大 | 较旧 70%→`<conversation_snapshot>` XML user 消息；保留 30%；两阶段自校验 | `COMPRESSION_PRESERVE_THRESHOLD=0.3`；`RECENT_TURNS_PROTECTED=2` | chars/4 启发式（仅媒体时用 countTokens） | 4 层：遮→盘（50k/30k）、蒸馏（10k）、反向预算截断（50k）、文件 FULL/PARTIAL/SUMMARY/EXCLUDED | XML 唯一记忆 + 注入防御；磁盘卸载；专门总结模型 |
| **pi-agent** | `contextTokens > contextWindow − 16384` [无 turn 限制] | Append-only session；重建上下文 = 摘要 + 保留尾部 | `keepRecentTokens=20000` | chars/4 + provider-usage 锚点 | 总结时上限 2000 字符；保留尾部完整 | `/compact`；`/tree` 分支总结；累积 `<read-files>/<modified-files>`；split-turn 双重摘要 |
| **SWE-agent** | Tokenizer 溢出 → `ContextWindowExceededError` → **退出**（不总结） | 确定性历史处理器链（省略/截断/tag/缓存） | 按 processor（论文 n=5；缓存最后 2） | 精确 litellm tokenizer | 写入时上限 `max_observation_length=100_000` | 可组合处理器；prompt-caching；`windowed`+closed-window；成本/调用退出护栏 |
| **OpenHands** | EVENTS（`>max_size`）/TOKENS/REQUEST → SOFT/HARD | 总结中间，保留头+尾逐字（滚动窗口） | `keep_first`(4) + 尾部到 `max_size//2`（=40） | 精确 litellm `get_token_count` | Observations 作事件；`forgotten_event_ids` 集合折叠整个工具结果 | **Hard/Soft 需求**；`PipelineCondenser`；manipulation-index 对齐；非破坏 `Condensation` |
| **Continue** | 每请求 token 预算（无比例） | 丢最旧截断（无 LLM） | 最后 user/tool 序列 + system + tools 总保留 | js-tiktoken / Llama BPE；BASE=4 | 内联 `tool` 消息 +10；工具调用感知剪枝 | 安全缓冲 + 最小输出预留；context-providers 正交 |
| **Goose** | `DEFAULT_COMPACTION_THRESHOLD=0.8`；env 覆盖；context 错误反应式 | LLM 总结 → 单条 user 消息；可见性元数据重写 | 最新 user prompt 逐字 | 精确 tiktoken o200k + LRU 缓存 | 渐进移除工具响应；逐工具配对总结器（batch 10） | Append-only 可见性重写；audience-aware；结构化摘要 |
| **Cline** | 0.9 × 可用输入预算；回退 128k | Agentic LLM 摘要 或 basic 确定性折叠；投影 sidecar | `DEFAULT_PRESERVE_RECENT_TOKENS=20000` | chars/3 启发式 | 截断到 2000 字符；提取读/编辑范围；FileContextTracker | 溢出恢复模式；压缩 sidecar 投影 |
| **Roo Code** | `contextPercent >= autoCondenseContextPercent`（默认 100%）或 >0.9*window−预留；按 profile 覆盖 | Fresh-start LLM 摘要到 user 消息；滑动窗口截断（50%）回退 | 仅摘要（+ 命令块 + 折叠文件签名 50k） | Provider 原生 `countTokens()`（+tiktoken 回退） | 工具→文本用于摘要；文件折叠为签名（tree-sitter） | 非破坏 condense + shadow-git 检查点；context-error 25% 截断+重试 |
| **Cursor** | "窗口填满" [无 %] | 总结 Conversation bucket；history-as-file | 未知 | 每模型窗口（200k–1M）；tokenizer 未知 | 第三方输出→文件，tail/重读 | History-as-file；self-summarization 内嵌 Composer |
| **Copilot CLI** | **80% 自动，约 20% 缓冲，95% 暂停** | 结构化摘要替换历史 + 检查点 | 未量化 | 未文档化（/context 分解） | 工具结果被 20% 缓冲吸收 | 可审计检查点；非阻塞后台压缩 |
| **Amp** | **无**（手动 Handoff/Fork/Edit-Restore） | 第二模型蒸馏（handoff、`read_thread`）；不重写 | 整条线程保持短；无缓冲概念 | 500 行/2KB 每行截断（@文件） | 线程文本内工具输出；保持线程短 | "Agents get drunk"；短线程纪律；第二模型选择性提取 |
| **Devin** | 自动 + `/compact` [无 % 公开] | 自动压缩（多轮）；`/recap`、`/handoff` | 未知 | 未文档化 | Shell scrollback 约 3500 行，中间省略 | **Knowledge** 在窗口外加上下文（Trigger Description，repo 可固定） |

---

## 对轻量上下文管理模块的关键启示

1. **触发分层设计**：事件计数预算（OpenHands `max_size`）、窗口 token 比例（Copilot 80%、Gemini 50%、pi `window−16384`）、超阈值 token（Aider）、或纯每请求预算（Continue）。Roo/Cline 用"可用窗口百分比 + 预留输出缓冲"。

2. **两类机制家族**：(a) *LLM 总结*进 user/合成消息（pi、Gemini、Goose、OpenHands、Cline、Roo）；(b) *确定性截断/省略*（Continue、SWE-agent、Cline-basic、Copilot）。总结保留语义但有成本；截断免费但有损。

3. **最近缓冲是最重要旋钮**：每个总结器都保留逐字尾部——pi 20k、Cline 20k、Gemini 30%、Aider 约半预算、OpenHands 约 40 事件、Goose 最新 user prompt、Roo 仅摘要。

4. **Token 计数**：稳定模式是便宜 chars/4（或 chars/3 以偏保守）启发式锚到 provider `usage`（如可用）。精确 tokenizer（Aider/litellm、SWE-agent、Continue tiktoken、Roo 原生）成本更高但硬限决策更精确。混合（pi：启发式 + usage 锚点）对轻量 agent 是好的成本/性能权衡。

5. **工具输出处理是最大的上下文贡献者**：建议把大输出写盘（Gemini）或封顶（pi 2000、Cline 2000、SWE-agent 100k），并让工具结果保持与其工具调用配对（绝不切半——pi、Cline、Roo、OpenHands 都强制边界吸附）。

6. **非破坏设计是现代常态**：pi（append-only）、OpenHands（Condensation event）、Goose（可见性元数据）、Cline（sidecar）、Roo（condenseParent）——都保持耐久记录完整以支持 rewind/检查点。`Condensation`-event（OpenHands）+ soft/hard 需求是最可复用的架构。

---

## 真正未知的常量（在设计文档中说明）

Cursor（%、缓冲、tokenizer）、Copilot agent（窗口大小、tokenizer）、Amp（tokenizer 方法）、Devin（%、缓冲、tokenizer）。**不要**把有争议的 Copilot"约 30–40% 预留输出"当作事实。

---

## 参考来源

- Aider: https://github.com/paul-gauthier/aider（`aider/coders/base_coder.py`、`aider/history.py`、`aider/models.py`、`aider/repomap.py`）+ https://aider.chat/docs/repomap.html
- Gemini CLI: https://github.com/google-gemini/gemini-cli（`packages/core/src/context/chatCompressionService.ts`、`tokenLimits.ts`、`context/config/profiles.ts`、`toolOutputMaskingService.ts`、`toolDistillationService.ts`、`contextCompressionService.ts`、`utils/tokenCalculation.ts`）
- pi-agent: https://github.com/earendil-works/pi（`packages/agent/src/harness/compaction/compaction.ts`、`session/context.ts`、`messages.ts`、`utils.ts`）+ https://pi.dev/docs/latest/compaction
- SWE-agent: https://github.com/princeton-nlp/SWE-agent（`sweagent/agent/models.py`、`agents.py`、`history_processors.py`）+ `docs/reference/history_processor_config.md`
- OpenHands: https://github.com/OpenHands/software-agent-sdk（`openhands-sdk/openhands/sdk/context/condenser/`）+ https://docs.openhands.dev/sdk/arch/condenser
- Continue: https://github.com/continuedev/continue（`core/llm/countTokens.ts`、`constants.ts`、`core/context/providers/`）
- Goose: https://github.com/aaif-goose/goose（`crates/goose-context-management/src/lib.rs`、`crates/goose/src/context_mgmt/mod.rs`、`summarize.rs`、`token_counter.rs`）
- Cline: https://github.com/cline/cline（`sdk/packages/core/src/extensions/context/compaction-shared.ts`、`compaction.ts`、`sdk/packages/shared/src/llms/tokens.ts`）
- Roo Code: https://github.com/RooCodeInc/Roo-Code（`src/core/context-management/index.ts`、`src/core/condense/index.ts`、`foldedFileContext.ts`、`ShadowCheckpointService.ts`）+ https://roocodeinc.github.io/Roo-Code/features/intelligent-context-condensing/
- Cursor: https://cursor.com/blog/dynamic-context-discovery
- Copilot CLI: Microsoft Learn（自动压缩比例）
- Amp: https://ampcode.com
- Devin: https://docs.devin.ai
