# OpenAI Codex（开源）— LLM 上下文管理调研

> 调研目的：为 agent_lite 的上下文管理模块提供参考。
> 来源：开源仓库 https://github.com/openai/codex（`main` 分支），实仓路径 + OpenAI 官方 Responses API 文档 + 2026-01 工程博客 "Unrolling the Codex agent loop"。
> 注意：`codex-agent-sdk`（github.com/openai/codex-agent-sdk）仓库本次 GitHub API 返回 404，**无法核实其内容**；SDK 仅通过 Responses API 规范覆盖。所有标为 UNVERIFIED 的结论均已显式标注。

---

## 关键解码器（Topic 7，驱动一切）：Codex **不用** tiktoken/cl100k_base，用「字节启发式」

- `codex-rs/utils/string/src/truncate.rs`: `const APPROX_BYTES_PER_TOKEN: usize = 4;` 和 `approx_token_count(text) = ceil(len/4)`（`len.saturating_add(3)/4`）。
- 到处用作 `approx_token_count` / `approx_tokens_from_byte_count`（`codex-rs/utils/output-truncation/src/lib.rs`；`codex-rs/core/src/context_manager/history.rs`）。
- `history.rs` 明确说："用字节启发式估算 token......这是个粗略下界，不是 tokenizer 精确计数。"
- 图片估值是 patch 进去的常量：`RESIZED_IMAGE_BYTES_ESTIMATE = 7373`（约 1,844 tokens）、`ORIGINAL_IMAGE_PATCH_SIZE = 32`、`ORIGINAL_IMAGE_MAX_PATCHES = 10_000`（history.rs）。注释引了 platform docs。所以这是近似，不是精确 token 化。

---

## Topic 1 — 成本 / token 预算记账（每 turn / agent run）

- 单项估值：`estimate_item_token_count` 先 JSON 序列化一个 ResponseItem，再把字节→token（4 字节/token 启发式）（history.rs）。token 计数按历史项求和。
- 总用量：`ContextManager::get_total_token_usage(server_reasoning_included)` 返回 `last_token_usage.total_tokens`（来自 API）加上「最后一条模型生成项之后追加的项」的 token，外加（除非服务端已计入）非最后一条的 reasoning token（history.rs）。
- 会话 turn 暴露 `ContextWindowTokenStatus`（`codex-rs/core/src/session/context_window.rs`）：字段 `active_context_tokens`、`auto_compact_scope_tokens`、`auto_compact_scope_limit`、`full_context_window_limit`、`base_window_tokens_remaining`、`token_limit_reached`。
- 完整上下文窗口 = `resolved_context_window() * effective_context_window_percent / 100`（context_window.rs）—— 即可配置百分比，不是原始窗口。
- "max_turns"：每 turn **没有** `max_turns` token 预算常量；预算是 token 制，不是 turn 数。另有独立的共享会话 rollout 预算：`RolloutBudget`（`codex-rs/core/src/rollout_budget.rs`）从 `usage.codex_rollout_budget_units` 或 `output_tokens*sampling_token_weight + non_cached_input*prefill_token_weight` 累计 `weighted_tokens_used`，一旦 `>= config.limit_tokens` 返回 "exhausted"。在 `reminder_at_remaining_tokens` 阈值触发提醒。
- 配置旋钮（`codex-rs/config/src/config_toml.rs` + `codex-rs/core/src/config/mod.rs`）：`model_context_window`、`model_auto_compact_token_limit`、`model_auto_compact_token_limit_scope`、`tool_output_token_limit`。

---

## Topic 2 — 自动压缩 / 汇总

- **触发**：当 `ConfigWindowTokenStatus.token_limit_reached` 为 true 时（context_window.rs），其中 `token_limit_reached = buffered_auto_compact_limit is Some == auto_compact_scope_tokens >= limit || full_context_window_limit_reached`。
- **机制是历史重写**，不是逐 turn 的摘要消息注入。压缩后，旧 `input` 被一个新的、更小的 item 列表替换。压缩摘要是 user-role 消息，携带 `SUMMARY_PREFIX` + 模型摘要；整个先前的对话被丢弃。
- 摘要文本 = `format!("{SUMMARY_PREFIX}\n{summary_suffix}")`（`codex-rs/core/src/compact.rs`）。
  - `SUMMARIZATION_PROMPT`（`codex-rs/prompts/templates/compact/prompt.md`）："You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task. Include: current progress and key decisions; important context/constraints/user preferences; what remains to be done (clear next steps); any critical data/examples/references..."（经 `include_str!` 引入）。
  - `SUMMARY_PREFIX`（`codex-rs/prompts/templates/compact/summary_prefix.md`）："Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work..."
- **保留什么**：`collect_user_messages` 只保留真正的 user 消息（丢弃摘要消息），`build_compacted_history` 保留最近 user 消息，上限 `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`（compact.rs）。然后把摘要作为**最后一条 user 消息**追加。保留 `retained_image_count`（分析），v2 路径按图片预算保留图片/音频。
- 压缩历史重注入：`InitialContextInjection` 枚举（compact.rs）：`DoNotInject` 用于 pre-turn/手动（替换历史 + 清空 `reference_context_item`，所以下一 turn 完全重新注入初始上下文）；`BeforeLastUserMessage` 用于 turn 中途压缩（模型被训练成把摘要当最后一项，故初始上下文插在最后一条真实 user 消息上方）。放置逻辑在 `insert_initial_context_before_last_real_user_or_summary`。
- 压缩后：`sess.replace_compacted_history(...)` 然后 `sess.recompute_token_usage(...)`（compact.rs、compact_remote.rs、compact_remote_v2.rs）。
- 压缩后警告（compact.rs）："Heads up: Long threads and multiple compactions can cause the model to be less accurate. Start a new thread when possible..."
- 分析：`CompactionAnalyticsAttempt` 记录 `active_context_tokens_before/after`、`compaction_summary_tokens`、`cached_input_tokens`、`cache_write_input_tokens`、`retained_image_count`；策略是 `CompactionStrategy::Memento`（compact.rs）。

---

## Topic 3 — 早前消息的折叠 / 截断

- 主要机制是 `thread_rollout_truncation.rs`（`codex-rs/core/src/thread_rollout_truncation.rs`）：按「user turn」边界的截断。
  - `truncate_rollout_before_nth_user_message_from_start(items, n_from_start)` 在第 n 条 user 消息前切断（折叠更早的 turn），保留近期的。
  - `truncate_rollout_to_last_n_fork_turns(items, n)` 只保留最后 N 个 fork-turn 边界，丢弃 pre-turn 启动上下文。
  - `truncate_rollout_after_turn_id` / `truncate_rollout_before_turn_id` 在持久 `TurnStarted` 边界切断。
  - `user_message_positions_in_rollout` 和 `fork_turn_positions_in_rollout` 应用 `ThreadRolledBack` 标记，使索引反映回滚后的历史。
- 在 ContextManager 层，`drop_last_n_user_turns(num_turns)`（`codex-rs/core/src/context_manager/history.rs`）从内存模型历史丢弃最后 N 个 instruction turn，镜像 thread-rollback 语义；若它裁剪了一个混合 `build_initial_context` developer 包，就清空 `reference_context_item` 使下一 turn 完全重注入上下文。
- 上下文 pre-turn 项从模型可见历史过滤掉（history.rs 的 `items()` 丢弃 `role=user` 的 contextual 片段）。所以"折叠"保留近期真实 turn，折叠/隐藏更旧的 contextual developer/user prefix 项。
- 注意：**没找到字面叫 "folding" 的模块**。替换旧 turn 的机制是 (a) turn 边界截断（thread_rollout_truncation.rs）或 (b) 压缩（compact.rs）。没匹配到 "fold"/"collapsed"/"placeholder"。

---

## Topic 4 — 工具输出管理

- 工具输出作为 `FunctionCallOutput` / `CustomToolCallOutput` 项存储，入库时用 `TruncationPolicy`（bytes 或 tokens）截断。
- 在 `record_items_with_metadata`（history.rs）：对工具输出应用 `truncate_function_output_payload`，策略为 `policy * 1.2`（或 `fallback_token_limit_override`），1.2× 是工具序列化开销。含 `estimate_audio_token_count`。
- `TruncationPolicy`（`codex-rs/utils/output-truncation/src/lib.rs`）支持 `Bytes` / `Tokens`。`truncate_text` 中间截断，保留前缀+后缀，带标记如 "…N tokens truncated…"（truncate.rs `truncate_middle_with_token_budget`）。
- **上下文窗口驱动的裁剪**：`trim_function_call_history_to_fit_context_window`（`codex-rs/core/src/compact_remote.rs`）从最新的工具输出组往回走；若估 token > 上下文窗口，把每个 `FunctionCallOutput`/`CustomToolCallOutput`/`ToolSearchOutput` 重写为 `truncated_output_payload`（替换正文字体为 `CONTEXT_WINDOW_TRUNCATED_OUTPUT_MESSAGE = "Output exceeded the available model context and was truncated"`）。返回 `(rewritten_outputs, estimated_deleted_tokens)`。
- 配置：`tool_output_token_limit`（config_toml.rs）。
- 工具 call/output 配对在 `normalize_history`（history.rs）里经 `ensure_call_outputs_present` / `remove_orphan_outputs` 强制；不支持的图片/音频经 `strip_images_when_unsupported`/`strip_audio_when_unsupported` 剥离。
- **注意**：工具输出（及 shell/web-search/image-gen 调用）跨压缩默认**不保留**——`should_keep_compacted_history_item`（compact_remote.rs）丢弃所有工具输出；assistant 消息与压缩项幸存。

---

## Topic 5 — 压缩模块（codex-rs/core/src/compact.rs）

- 入口：`run_inline_auto_compact_task`（自动，用 SUMMARIZATION_PROMPT，CompactionTrigger::Auto）、`run_compact_task`（手动 /compact，CompactionTrigger::Manual，CompactionReason::UserRequested，CompactionPhase::StandaloneTurn），外加远程变体 `run_remote_compact_task`（compact_remote.rs）和 v2（compact_remote_v2.rs）。
- 机制：捕获历史 → 前置 prompt 输入 → 流式到模型 → 在 `response.completed` 拿到摘要 → 构建新压缩历史 → 按 `InitialContextInjection` 重注入初始上下文 → `replace_compacted_history` → `recompute_token_usage` → 发 `ContextCompaction` turn 项 → 警告。
- 重试/退避：可重试错误时退避 `backoff(retries)`，上限 `stream_max_retries()`；`CodexErr::ContextWindowExceeded` 时移除最旧历史项（`history.remove_first_item()`，经 `normalize::remove_corresponding_for` 也逐出配对的 call/output）以保留前缀缓存，并重试（compact.rs）。`SessionBudgetExceeded` 则中止。
- 压缩被建模为带前后 hook 的生命周期（`run_pre_compact_hooks`/`run_post_compact_hooks`）和 `ContextCompaction` turn 项。
- 压缩内 token 计数用 `approx_token_count`（user 消息预算）和 `estimate_item_token_count`（历史项）。
- 远程 v2（compact_remote_v2.rs）：`RETAINED_MESSAGE_TOKEN_BUDGET = 64_000`（镜像服务端默认）、`MAX_RETAINED_AGENT_MESSAGE_TOKENS = 10_000`、`MAX_REMOTE_COMPACTION_V2_STREAM_RETRIES = 2`。保留消息在 64k 预算内按"最新优先"截断；过滤掉发展性 progress/completion 消息与超大 agent 消息；在图片预算下计保留输入图片。
- **Token-budget 压缩**（compact_token_budget.rs）是独立路径：完全跳过模型/服务端汇总，只"安装一个全新上下文窗口" via `start_new_context_window`，仍发 ContextCompaction 生命周期事件。

---

## Topic 6 — Responses API 上下文处理 vs 客户端

- Codex **每次都把完整对话作为 `input` 发送**；它**不用** `previous_response_id`。据 2026-01 工程博客："虽然 Responses API 支持可选的 `previous_response_id` 参数来缓解此问题，但现在 Codex 不使用它，主要是为了保持请求完全无状态并支持 Zero Data Retention (ZDR)。" 某些场景的服务端密钥解密保留先前 turn 的加密推理。
- **Prompt 结构**（博客）：`instructions`（system/developer，来自 `model_instructions_file` 或 base_instructions）、`tools`、`input`。静态内容（instructions/system、tools）放前，可变内容放后，以最大化前缀缓存命中。Responses API 服务端重排前三个项；客户端控制 tools+instructions+input。
- 因为 Codex 重发完整历史，API 服务端**单独**管理服务端压缩：`/responses` 支持 `context_management` + `compact_threshold`（服务端压缩），外加独立 `/responses/compact` 端点，返回 item 列表含 `type=compaction` 项，带 opaque `encrypted_content`。Codex 新自动压缩用此端点。
- Responses API 对话状态指南（developers.openai.com）：管理上下文要么 (a) 每次传整个先前 `output`/messages（`store:false`），(b) `previous_response_id` 链式，或 (c) 更新的 `conversation` 对象（持久对话 id，无 30 天 TTL）。用 `previous_response_id` 时，"链中所有先前响应都为输入 token 计费。" reasoning 用 `reasoning.context: "all_turns"`，重放完整输出保持 reasoning/`phase` 完整。
- 客户端 vs 服务端拆分：token 记账在 `response.completed` 返回（`TokenUsage { input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens, codex_rollout_budget_units }`），Codex 经 `update_token_usage_info` / `record_observed_response_completed` 和 `record_rollout_budget_usage` 记录。

---

## Topic 7 — Token 计数

- 启发式，约 4 字节/token（ceil）。codex-rs **没有** tiktoken/cl100k_base 依赖。唯一例外是纯启发式：图片/音频按 modality 打 patch 估算（见 history.rs 常量），reasoning/加密压缩内容长度调整（`estimate_reasoning_length`、`estimate_encrypted_function_output_length`）。

---

## Topic 8 — Prompt 缓存与前缀不变量

- Codex 保持旧 prompt 是新 prompt 的**精确前缀**（"old prompt is an exact prefix of the new prompt... this is intentional... enables prompt caching"）。缓存命中只在精确前缀匹配；静态内容在前、可变内容在后（博客 + responses 文档）。
- 缓存 miss 原因（博客）：改 `tools`、改 `model`（改模型特定 instructions 项）、改 sandbox config/approval mode/cwd。
- Codex 处理会话中配置变化是**在 `input` 里追加**新消息而非编辑较早的（新 `role=developer` permissions 消息；新 `role=user` environment_context 消息）——以保留缓存前缀（博客）。Bug 教训：MCP 工具必须按一致顺序枚举，否则会使缓存失效。
- 压缩**故意保留前缀**：压缩时遇到 ContextWindowExceeded 会从头部 `remove_first_item()`（"Trim from the beginning to preserve cache (prefix-based) and keep recent messages intact"——compact.rs）。world-state 更新作为 diff/patch 发送（`WorldStateItem::patch`）对 `world_state_baseline`，`render_history_diff` 只发变更片段（history.rs、token_budget_context.rs）以保持稳定前缀完整。
- 缓存 token 指标记录在压缩分析里：`cached_input_tokens`、`cache_write_input_tokens`（compact.rs / compact_remote_v2.rs）。

---

## Topic 9 — 上下文用量如何呈现给用户

- 模型可见的提醒作为 **`developer`-role 的 contextual 片段**注入——不是 UI 的 "%"：
  - `TokenBudgetRemainingContext.body()` → "You have {tokens_left} tokens left in this context window."（`codex-rs/core/src/context/token_budget_context.rs`），用 `ContextWindowTokenStatus.base_window_tokens_remaining`。
  - `TokenBudgetReminder` → `reminder_message_template` 代入 `{n_remaining}`，当 `base_window_tokens_remaining <= reminder_threshold_tokens` 触发（`codex-rs/core/src/session/token_budget.rs`）。
  - `AutoCompactFallbackPrompt` 在 `base_window_tokens_remaining == 0` 时注入（token_budget.rs）。
  - `RolloutBudgetContext.body()` → "You have {} weighted tokens left in the shared session token budget."（`codex-rs/core/src/context/rollout_budget.rs`）。
  - `ContextWindowGuidance`（world_state/context_window_guidance.rs）发 REPLACEMENT/REMOVAL 通知到 `<...>` developer 块，让模型看到当前引导。
  - `CompactionSummary` 作为 `user`-role 片段注入（`content_kind = "compaction.summary"`，compaction_summary.rs）。
- 客户端/协议层：`ThreadTokenUsageUpdatedNotification`（`codex-rs/app-server-protocol/schema/json/v2/ThreadTokenUsageUpdatedNotification.json`）推送 token 用量给 app-server/extension 客户端；context_window.rs 的 `ContextWindowTokenStatus` 是驱动它的源。TUI/app 的"context: 45%"指示器从这些值渲染——但 **我未在开源 Rust 核心找到确切百分比格式串**（这是前端/扩展关注点）；所以那个"% 指示器"声明在**源码里 UNVERIFIED**，可能藏在闭源 app / IDE 扩展 / web 前端。
- 手动压缩：`/compact` 斜杠命令（历史路径在工程博客确认；目前实现为 `run_compact_task`/`run_remote_compact_task`/`run_manual_compact_task`，带 `CompactionTrigger::Manual`、`CompactionReason::UserRequested`，在 compact.rs / compact_remote.rs / compact_token_budget.rs）。上下文用尽得到错误："Codex ran out of room in the model's context window. Start a new conversation or clear earlier history before retrying."（openai/codex issue #7808 报告）。

---

## 不确定性 / 未核实

- `codex-agent-sdk` 仓库内容：GitHub API 返回 404，无法确认路径。SDK 声明仅按 Responses-API-规范推导。
- TUI/app 的"context % 指示器"确切字符串无法在开源 Rust 核心定位（前端/扩展关注点）；底层 token-remaining 数字在核心里有。
- 无名为 "folding" 的模块；更早 turn 的替换由 turn 边界截断（`thread_rollout_truncation.rs`）和压缩（`compact.rs`）完成，非字面 placeholder/"fold"。

---

## 参考来源

- 仓库内部路径（见上文各 Topic）
- https://openai.com/index/unrolling-the-codex-agent-loop/
- https://developers.openai.com/api/docs/guides/conversation-state
- https://github.com/openai/codex（issue #7808）
