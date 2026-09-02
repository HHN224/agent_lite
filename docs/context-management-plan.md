# agent_lite 上下文管理 + 压缩 —— 轻量实现计划

> 依据：`docs/research/05-synthesis-and-recommendations.md` 的落地建议，并对照当前源码接缝实读。
> 定位：**MVP 优先、先做出来、再慢慢改**。方向性说明，不落到具体代码。
> 覆盖目标：让上下文管理**真正生效**（能计量、能触发、能压缩、能管工具输出），并把 `/compact` 从未实现变成真压缩。

---

## 0. 一句话目标

给 agent_lite 补上「策略层」：在保留现有 append-only 数据层的前提下，驱动出 **①触发 ②压缩 ③保留 ④计量 ⑤工具输出管理** 这五件能独立测试、独立替换的事。默认走**非破坏 + 边界吸附 + 独立摘要调用**的轻量路径，先让它跑通，再谈精确与缓存。

---

## 1. 已锁定的 5 个设计决策（沿用你列出的权衡）

| # | 决策 | 轻量做法（本计划采用） | 备注 |
|---|---|---|---|
| 1 | 摘要放 **user 消息** | 把压缩 summary 渲染成一条 `user` 消息，置于 surface 历史头部（**替换**当前的"第二条 system 消息"） | 改动点在 `build_llm_payload`，会动一个现有测试；见 §3.2 的"接缝注记" |
| 2 | 压缩**不破坏原始记录** | append-only：被遮区间**仍在** entries 里，只是不再发给模型；用"总结的压缩"合并多次压缩 | 数据层已具备，别推翻 |
| 3 | 工具输出 **"叠加头尾"** | 超阈值就把 `head + 分隔标记 + tail` 叠起来回填，保留 `tool_call_id` 配对 | 最省 token 的一步，先做这个，写盘 preview 列为后续 |
| 4 | 摘要输出走**独立 provider 调用** | 压缩时单独调一次模型（**不复用** agent loop 的 turn / request） | 用固定结构化摘要 prompt |
| 5 | token 计量用**启发式 + provider usage 锚点** | 优先复用最近一次真实 `usage`，只对之后增量用 `chars/4` 启发式 | provider 需能暴露 `usage`（见 §4 风险） |

> **接缝注记（第 1 条）**：现状 `build_llm_payload()` 与测试 `test_build_payload_with_compaction_uses_kept_start` 都把 summary 当**第二条 system 消息**渲染。若要按你锁定的放进 **user 层**，需要改动该方法（把 summary 从 system 改成 user），并同步该测试。两种放法在这里都是一行量级，都属于"走一步看一步"的小决策；若将来要做前缀缓存，建议在 user 层（DSH 等主流做法）。

---

## 2. 现状接缝盘点（代码在哪，可直接落针）

- `agent_core/session.py` —— 数据层已就绪：`SessionEntry(type="compaction")` + `append_compaction()` + `build_llm_payload()`（已渲染 summary + `first_kept_entry_id` 起的保留消息）+ `estimate_tokens()`（chars/4，可注入）。**缺**：真正的"遮盖/合并"语义、工具配对平衡校验。
- `agent_core/agent.py` —— `Agent.prompt()`：每轮先 `session.build_llm_payload()` 再交给 loop。**缺**：给模型前没有触发压缩的钩子。
- `agent_core/loop.py` —— 工具结果在 `step()` 里直接 `result.content` 全文回填。**缺**：工具输出截断。
- `agent_core/tool_executor.py` —— 统一执行器，结果规范化后返回 `ToolResult(content, ...)`。是插入剪枝的天然位置。
- `ai/providers.py` —— `LLMProvider.stream()` 契约，`OpenAIProvider` 目前**不采集 usage**（需开启 `stream_options.include_usage`）。是"usage 锚点"的接入点。
- `coding_agent/__main__.py` —— `/compact` 目前只 `print("尚未实现")`。替换点。
- `tests/` —— 有 `FauxProvider`（剧本化假 Provider），可**确定性测试**压缩/触发/截断，不碰真实 API。

---

## 3. 落地步骤（分 3 段，每段结束都可独立运行、可测试）

### 阶段 A —— 计量 + 触发（先让"要不要管"能回答）

**做什么**
1. 抽一个 `TokenMeter`：`measure(session, context_window) -> pressure`，内部用"最近真实 usage 锚点 + 之后增量启发式"估算当前上下文占用；估算函数可注入（复用/替换现有 `estimate_tokens`）。
2. 加 `should_compact(session, context_window) -> bool`：默认 `context_tokens > context_window * threshold_ratio`（起点 0.8）。
3. 把钩子接进 `Agent.prompt()`：在**每次 build payload 之前**调用 `should_compact`，若真就触发阶段 B 的压缩后再重建 payload。

**为什么先做这步**：把"计量"与"压缩引擎"解耦（DSH 的思路），计量器可单独测试，也是将来 UI 显示"上下文占用"的基础。改动最小、风险最低。

**产出/可验收**：调用 `measure` 能得到接近真实的占用数；构造一个超阈值的 session，`should_compact` 返回 True；FauxProvider 下可断言行为。

---

### 阶段 B —— 真正的压缩（模块主体，把 `/compact` 做真）

**做什么（核心是"边界吸附 + 保留尾部 + 独立摘要 + 非破坏落地"）**
1. **切点必须工具配对平衡（最重要）**：遍历到 head 的链，找到**最后一个安全的边界**——边界处不能是"assistant 已发 tool_calls 但 tool 结果未回填"的区间；通常选"一条普通消息"或"一条 tool_calls 已补齐结果的 assistant"。绝不在此处切。
2. **保留尾部（工作记忆）**：保留最近 `keep_recent_tokens`（起点 20000）的**原文**，作为近期上下文。
3. **摘要旧区间**：取"切点之前的所有消息"，**走一次独立 provider 调用**（不走 loop 的 turn），用固定结构化摘要 prompt 生成 summary（比自由摘要更稳定）。调用时**重发系统 prompt + 工具 schema + 被遮区间消息 + 摘要指令**（若 provider 有前缀缓存则再回放前缀，没有就简化）。
4. **非破坏落地**：调已有的 `session.append_compaction(summary, first_kept_entry_id)`，让 `build_llm_payload()` 自动渲染成"summary（user 消息）+ 从 first_kept 起的保留消息"。旧消息仍在 entries 里（非破坏）。
5. **解决"多次压缩越攒越多"**：轻量起见，先做到**再次压缩时把上一个 compaction 节点也当作"被压缩对象"**（即"总结的压缩"），保持非破坏；若想更省内存，也可在压缩后真正丢弃被遮区间（强破坏，后续再权衡）。两者都先留开关。
6. **存档**：压缩成功后 `repo.save(session)`。
7. **接入 `/compact`**：替换 `__main__.py` 里 `print("尚未实现")` 的 stub，改为调用压缩引擎的 `compact_now(session)`。

**产出/可验收**：`/compact` 真的能压；压缩后 payload = system → [summary user 消息] → 保留原文；切点绝不在未闭合工具区间上；多次压缩后可用语义仍完整（非破坏）；FauxProvider 下可断言压缩前后 payload 的变化。

---

### 阶段 C —— 工具输出"叠加头尾"截断（最省 token）

**做什么**
1. 在工具结果回填处（`loop.py` 回填 / `tool_executor.py` 结果规范化）对 `result.content` 截断：超过 `tool_threshold_chars`（起点 8192）就把 `head（4096）+ 分隔标记 + tail（1024）` 叠加后回填。
2. **关键**：截断只动内容，**保留 `tool_call_id` 与 assistant 的 tool_calls 配对关系**，保证历史不坏。
3. （后续可选）把完整结果单独写到 `sessions/<id>/tool-results/<call_id>.txt`，模型只见 preview + 路径；本计划**先跳过**，直接用叠加头尾。

**为什么先做这步**：工具输出常是最大上下文贡献者，这一步投入最小、省 token 最明显。

---

## 4. 默认参数（起点，全部做成配置项，别硬编码）

| 参数 | 起点 | 依据 |
|---|---|---|
| `threshold_ratio` | 0.8 | DSH / Goose 默认；比 Claude Code 的激进值（~0.93）保守，兼顾 context rot |
| `keep_recent_tokens` | 20000 | pi / Cline 一致 |
| `summary_max_tokens` | 8192 | DSH 默认 |
| `tool_threshold_chars` | 8192 | DSH pruner |
| `tool_head_chars` / `tool_tail_chars` | 4096 / 1024 | DSH pruner |
| `compaction_retries` / `overflow_retries` | 1 | DSH 默认 |

> **不要照抄某一厂商的精确数字**。这些是针对深蓝等模型起步的参考值，需按你的真实模型实测校准。

---

## 5. MVP 边界 —— 明确哪些**先不做**（后续再慢慢加）

- **溢出强制恢复**（provider 报 `context_window_exceeded` 时强制压一次）：列为后续。
- **熔断**（连续 N 次压缩失败则本会话跳过自动压缩）：列为后续。
- **前缀缓存感知**（让旧 prompt 是新 prompt 的精确前缀）：列为后续，属收益最高但代价最大的优化；非破坏 + 追加语义已部分朝这个方向靠。
- **工具结果写盘 + preview**（Claude Code 做法）：阶段 C 已说明先跳过，用叠加头尾。
- **中断/恢复合成**（恢复时给模型"TOOL_OUTCOME_UNKNOWN"等状态）：列为后续，需配合恢复模块。
- **多模态 token 计量**（图片固定计费）：当前无图片输入，先按 `chars/4` + usage 锚点即可，需要时再加。

---

## 6. 验收标准（怎样算"能正常管理上下文 + 压缩完成"）

**可测试（测试优先，用 FauxProvider 不碰真实 API）**
- `TokenMeter.measure`：对给定 session 返回合理占用；能优先用 usage 锚点、否则回退启发式。
- `should_compact`：超阈值返回 True，未超返回 False。
- 压缩：切点不落在未闭合工具区间；压缩后 payload 含 summary（user 消息）+ 保留原文；多次压缩语义仍完整（非破坏）；`/compact` 不再打印"尚未实现"。
- 工具截断：超大 `result.content` 被叠成 head+marker+tail，且 `tool_call_id` 配对保留。

**手动验收（一条长会话走通闭环）**
- 跑一个 token 会增长的会话，观察接近阈值时自动压缩、payload 从"全历史"变成"summary + 保留原文"；
- 手动 `/compact` 立即压缩已生效；
- 大工具输出被截断，模型仍能继续正常作答；
- 会话重启（load + build payload）后结构与压缩前一致，历史不坏。

---

## 7. 风险与注意（容易被忽略的正确性点）

1. **切点工具配对平衡**：压缩区间切在未闭合工具调用上会损坏模型历史——所有厂商都强调。这是压缩里最重要的正确性点，务必优先实现并被测试覆盖。
2. **usage 锚点的获取**：`OpenAIProvider.stream` 目前不采集 `usage`，需要开启 `stream_options={"include_usage": True}` 并在流末收集最终 usage；若某 provider 拿不到，就**整体回退到 chars/4 启发式**，别硬依赖。
3. **摘要调用回放前缀**：为复用前缀缓存，摘要调用应重发原 system prompt + 工具 schema + 被遮区间消息；无缓存则简化，不影响正确性。
4. **"上下文占用"只是展示不是门禁**：正确读取的是 `measure()`，`token_count` 元数据只作参考，别为精确计量过度设计。
5. **不要把数据层推倒重来**：`session.py` 的 events + compaction 数据模型 + 可注入估算 + 持久化已经打好了地基，本计划只补"策略层"，不要在数据层上做大改动。
