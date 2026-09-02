# 上下文管理模块设计 —— 综合报告 + agent_lite 落地建议

> 调研结论与设计建议。全篇为研究产物，**未改动 agent_lite 任何源码**。
> 调研对象：DeepSeek Harness（本地源码）、Claude Code（含泄漏源码）、OpenAI Codex（开源）、以及 Aider / Gemini CLI / pi-agent / OpenHands / SWE-agent / Continue / Goose / Cline / Roo Code / Cursor / Copilot agent / Amp / Devin。
> 单点 deep-dive 见 `01`~`04`；本文统合所有发现，直接落到 agent_lite 的代码接缝上。

---

## 0. 一句话结论

成熟厂商做"上下文管理"的本质是：**把一份「append-only 的事实源（会话日志）」投影出「模型实际看到的历史」，并在触发条件满足时，把一段旧的历史区间用一次"带锁的替换"折叠成一条摘要（或干脆确定性截断），同时永远保留最近的一段"工作记忆"原文，并且全程把「prompt 前缀」当作要尽力保持不变的宝贵资源（为了命中 KV cache）。**

这不是"给消息列表加个清空按钮"，而是 **6 个正交的关注点**，各自可独立设计/替换：

```
① 触发（Trigger）         何时该管？
② 压缩机制（Mechanism）  怎么管？摘要 or 截断？
③ 保留策略（Retain）     哪些"工作记忆"要原文保留？
④ Token 计量（Counting） 怎么算"快到窗口了"？
⑤ 工具输出处理（Tool out）大工具结果怎么进上下文？
⑥ KV Cache 感知（Prefix）怎么尽量不动 prompt 前缀？
```

---

## 1. 六家代表性厂商的横向对比（浓缩版）

| 关注点 | DeepSeek Harness | Claude Code | OpenAI Codex | pi-agent | OpenHands | Gemini CLI |
|---|---|---|---|---|---|---|
| ① 触发 | pressure=窗口×0.8，overflow=确认溢出 | 有效窗口−13000（200K→约167K，~93%） | `token_limit_reached`（auto_compact_scope ≥ limit 或 ≥ full_window） | `contextTokens > 窗口−16384` | EVENTS/TOKENS/REQUEST → SOFT/HARD | `/compress` 在 50% |
| ② 机制 | surface 替换为「摘要 user 消息 + 保留尾部」 | 摘要成 user 消息（`isCompactSummary`） | 整段历史重写 + 摘要 user 消息 | append-only session + 重建上下文 | 总结中间、保留头尾 | 旧70%→ `<conversation_snapshot>` XML |
| ③ 保留 | retainRatio=0.16（保留 16% 窗口） | session-memory 保留 10K–40K；微压缩 keepRecent=5 | 保留最近 user 消息 ≤ 20K token | `keepRecentTokens=20000` | `keep_first`=4 + 尾部 | 保留最近 30% / 65K token |
| ④ Token 计量 | chars/4 + provider-usage 锚点 | chars/4（JSON=2，图片=2000，×4/3） | 4 字节/token（ceil）+ 图片 patch | chars/4 + usage 锚点 | litellm 精确 | chars/4（媒体用 countTokens） |
| ⑤ 工具输出 | 剪枝器：head+marker+tail（8192/4096/1024 字符） | >50K 写盘，模型见 2000 字符 preview | 截断函数输出（policy×1.2） | 总结时截 2000 字符 | 整块折叠进摘要 | 4 层：遮蔽→磁盘→蒸馏→反向预算 |
| ⑥ KV Cache | 摘要调用**回放**前缀 + 追加指令 | 内容排序：system→上下文→对话；列明失效清单 | 旧 prompt 为新的精确前缀；change 只追加不编辑 | （append-only 保持前缀稳定） | — | — |

**共性最强的信号**：
- 几乎所有厂商都用 **chars/4 启发式**，且**优先复用 provider 上报的真实 usage**（pi、DSH、Claude Code）。
- 几乎所有 LLM 总结型机制都**保留一个"近期原文尾部"**（20K token 或 30% 或"半预算"）。
- 几乎所有机制都**把摘要放在 user 消息层**（少数放在 system），因为这样更像"用户补充的背景"，且不动 system prompt 前缀。
- 几乎所有实现都强调**工具结果与工具调用必须配对**（切点绝不能在未闭合的工具区间上）。
- **非破坏（append-only）是现代常态**：pi、OpenHands、Goose、Cline、Roo、DSH 都保留完整原始记录，压缩只是"替换给模型看的那层"。

---

## 2. agent_lite 现状盘点（代码接缝）

对照上面 6 个关注点，agent_lite 目前的**数据层已经做对了一大半**，但"驱动数据层的策略层"还没实现。关键代码接缝：

### 已有的（在 `agent_core/session.py`）
- ✅ **append-only 事件链**：`SessionEntry` 带稳定 `id` / `parent_id`，`head` 走到叶子。追加式、不可变（`test_append_does_not_mutate_old_entries` 已覆盖）。
- ✅ **压缩的数据模型**：`SessionEntry.type == "compaction"`，带 `summary` + `first_kept_entry_id`；`append_compaction()` 已有。
- ✅ **payload 重建已支持压缩**：`build_llm_payload()` = `system` → `最新 compaction 的 summary（作为第二条 system 消息）` → `从 first_kept_entry_id 起的保留消息`。
- ✅ **token 计数可注入**：`estimate_tokens()` 用 `chars/4`（`max(1, len/4)`），`_token_estimate` 钩子可换成任何实现。
- ✅ **持久化**：`SessionRepository` 存 `sessions/<id>.json`（原子 tmp 替换），`token_count` 序列化。

### 缺失的（这是你要开发的模块）
- ❌ **没有"触发判定"**：没有任何地方在 token 超阈值时自动压缩。`/compact` 只是 `print("尚未实现")`（`coding_agent/__main__.py:239`）。
- ❌ **没有 `SessionManager`**：注释里提到"真正的 LLM 总结由 SessionManager 的 compact() 驱动"，但该类还不存在。
- ❌ **压缩只做了"起始指针"模拟，没做真正的"替换/遮盖"**：`build_llm_payload` 从 `first_kept_entry_id` 开始发，旧消息只是"不再发了"，仍在 `entries` 里。多次头部压缩会**越攒越多**（每次压缩的"被包进去的旧消息"其实还在，只是各自带一个 summary 节点）。
- ❌ **没有工具配对平衡校验**：切点可能落在"assistant 已发起 tool_calls 但 tool 结果未回填"的区间上，重启后模型历史会坏。
- ❌ **工具输出无管理**：`ToolExecutor` 直接把 `result.content` 全文回填进 messages（`loop.py:158-162`），大输出直接灌进上下文。
- ❌ **未区分"模型历史（surface）"与"人类 transcript"**：`build_llm_payload` 直接给模型历史；UI/回放共用。

---

## 3. 给 agent_lite 的落地建议（分步、由简到繁）

> **目标**：让 agent_lite 在**不加重型框架**的前提下，获得"厂商级上下文管理"的能力。建议按下面 4 个递进阶段实现，每阶段都能独立生效、可测试。

### 阶段 A（地基，最小改动）—— 补一个 `ContextManager`，把"触发 + 计量"做成可注入策略

这对应 ①④ 两个关注点，全部可复用现有 `Session`。

1. **抽一个 `TokenMeter`（或复用 `estimate_tokens`）**：把 `chars/4` 的估算提升为**感知多模态**的版本——
   - 图片按固定值（pi/DSH 用 4800 字符 ≈ 1200 token；Claude Code 用 2000 token；可先取 1200）。
   - **优先复用 provider 真实 usage**：如果最近一条 assistant 消息带了 `usage`（DSH 的 `TokenUsage`），就用 `usage.input+cacheRead+cacheWrite+output` 作为锚点，只对之后的增量消息用启发式。这是 pi/DSH/Claude Code 的共同做法，能显著提高准确度。
   - 参考 pi `estimateMessageTokens` / `estimateContextTokens`（见 `01` §14 的具体实现）。
2. **加"溢出检测"**：`is_context_overflow(error)` 用一组正则匹配 provider 的错误串。参考 pi `overflow.js`（见 `01` §14）。至少覆盖 DeepSeek/OpenAI/Anthropic 三条。
3. **`ContextManager` 暴露两个操作**（对齐 DSH 的 `compactIfNeeded` / `compactNow`）：
   - `should_compact(session, context_window) -> bool`（压力判定，默认 `context_tokens > context_window * 0.8`）。
   - `compact(session, ...)`（真正的压缩动作，见阶段 B）。

> **为什么先做这一步**：DSH 的核心设计是"把计量（token-meter）与压缩引擎（compaction）解耦"。你可独立测试计量器，且它也是未来 UI 显示"上下文占用"的基础。

### 阶段 B（核心）—— 把 `/compact` 从"未实现"变成真压缩

这是模块的主体，对应 ②③ 关注点。**强烈建议采用"非破坏 + 边界吸附"模式**（pi/DSH/OpenHands 的做法）。

1. **切点必须工具配对平衡**（最重要！）：
   - 遍历 `session._path_to_head()`，找到**切点之前的最后一个"压缩边界"**。
   - 边界必须是：一条**没有未回填 tool_calls 的 assistant 消息**（即它的 tool_calls 之后都已补了 tool 结果），或一条普通 user/assistant 消息。
   - **绝不能在"assistant 有 tool_calls 但对应 tool 结果缺失"处切**。若数据层保证了 `record_turn` 的原子性，通常不会出现，但恢复/中断场景必须防御。
2. **保留尾部**：保留**最近 N 条**（默认 `keepRecentTokens = 20000`，或"最近 k 轮"）**原文**，作为"工作记忆"。参考 pi（20K）与 DSH（retainRatio 0.16）。
3. **摘要那段旧区间**：
   - 取"切点之前的所有消息"，调用 provider 做一次**独立的流式调用**（不走 `AgentLoop` 的 turn）。用**固定的结构化摘要 prompt**（参考 DSH 的 8 节检查点，见 `01` §6）。这种方式比自由摘要更稳定、更可复现。
   - **提示**：为了命中深蓝/OpenAI 的前缀缓存，摘要调用应**重发**原 system prompt + 工具 schema + 被遮区间消息，再追加摘要指令（DSH 的做法，见 `01` §4）。若你的 provider 无前缀缓存，可简化。
4. **落地压缩**：调用已存在的 `session.append_compaction(summary, first_kept_entry_id)`。这样 `build_llm_payload()` 自动把它渲染成"第二条 system 消息 + 从 first_kept 起的消息"。
   - 注意：这会**保留**旧消息在 entries 里（非破坏），符合现代范式。
   - 但**要解决"多次压缩越攒越多"**：要么每次压缩后**真正丢弃**被遮盖的旧消息（强破坏、省内存），要么让再次压缩时**把上一个压缩节点自己作为"被压缩对象"**（即"总结压缩的压缩"，DSH 的 `<compacted-summary>` 合并语义）。建议用后者，保持非破坏。
5. **成功后存档**：`repo.save(session)`。

### 阶段 C（省钱 + 省上下文）—— 工具输出管理

这是**最省 token 的一步**，对应 ⑤。工具输出通常是最大上下文贡献者。

1. **在 `ToolExecutor`（或 `loop.py` 回填处）对 `result.content` 做截断**：
   - 超过 `thresholdChars`（默认 8192）就替换为 `head + "\n\n[... tool result middle pruned ...]\n\n" + tail`（DSH pruner 的做法，见 `01` §8）。
   - 保留完整结果到一个**单独的存储**（可选：写到 `sessions/<id>/tool-results/<call_id>.txt`，模型只看到 preview + 路径，Claude Code 的做法）。这一步对最简单实现可先跳过，直接用叠加头尾即可。
2. **关键**：截断后**仍要保留与 tool_call 的配对关系**（`tool_call_id` 不变）。

### 阶段 D（可选进阶）—— 前缀缓存感知 + 溢出恢复

对应 ⑥ + 溢出场景。

1. **前缀保持**（参考 Codex）：尽量让"旧 prompt 是新 prompt 的精确前缀"。代价最高但收益最大的做法：**追加**新 context 而非编辑旧消息；配置变化用"新增一条消息"而非改旧消息。
2. **溢出强制恢复**（参考 DSH/Codex）：当收到 provider 的 `context_window_exceeded` 错误时，**无需再靠压力阈值**，直接强制做一次"最大均衡的头部缩减"，并**保留最新不可分割的单元**。重试授权以"surface 有前进"为条件。
3. **熔断**（参考 Claude Code）：连续 `MAX_CONSECUTIVE_FAILURES`（如 3）次压缩失败后，本会话剩余时间跳过自动压缩，避免无限重试浪费 API。

---

## 4. 推荐的默认参数（起点，可调）

| 参数 | 建议值 | 依据 |
|---|---|---|
| `threshold_ratio` | `0.8` | DSH 默认、Goose 0.8；Claude Code 更激进(约0.93)，但社区实测质量在 ~50–65% 就退化（context rot），0.8 是居中起点 |
| `retain_recent_tokens` | `20000` | pi / Cline 都用 20K |
| `retain_ratio`（若按比例） | `0.16` | DSH 默认 |
| `max_tokens`（摘要调用） | `8192` | DSH 默认 |
| `tool_threshold_chars` | `8192` | DSH pruner 默认 |
| `tool_head_chars` / `tool_tail_chars` | `4096` / `1024` | DSH pruner 默认 |
| `compaction_retries` | `1` | DSH 默认 |
| `overflow_retries` | `1` | DSH 默认 |
| `max_consecutive_failures` | `3` | Claude Code 熔断 |

> **不要照抄某一个厂商的精确数字** —— Claude Code 的 167K buffer、Copilot 的 80/20/95 都是**特定版本/模型**、且 Claude Code 的数字取自一次泄漏。建议把这些做成配置项，并针对你的实际模型（deepseek-v4-flash 等）**测试校准**。

---

## 5. 关键设计权衡（写代码前要定的）

| 决策 | 选项 A | 选项 B | 建议 |
|---|---|---|---|
| 摘要放哪层 | **user 消息**（Claude Code/Codex/Goose/Cline/Roo） | **第二条 system 消息**（agent_lite 现状） | 保持 agent_lite 现有"第二条 system"（改动小、已有测试），但**意识到**它会因"多一条 system 消息"而改变前缀。若将来做前缀缓存，可切换到 user 层。 |
| 压缩是否破坏原始记录 | **非破坏**（append-only，旧消息仍在） | 强破坏（真正删掉被遮消息） | agent_lite 已是非破坏，保持。用"总结压缩的压缩"解决越攒越多。 |
| 工具输出 | 叠加头+尾 | **写盘 + preview** | 先做"叠加头尾"（最简单），需要时再升到写盘。 |
| 摘要调用 | 独立 provider 调用 | 走 loop 的 turn | 独立调用（DSH/Codex 一致），不走 `agent/request`。 |
| Token 计量 | 纯启发式 | **启发式 + provider usage 锚点** | 用后者（pi/DSH/Claude Code 都这么做）。 |

---

## 6. 给 agent_lite 的实现地图（对照现有文件）

```
agent_core/
├── session.py          # 已有：事件链 + compaction 数据模型 + payload 重建 + token 估算
│     └── 需扩展：真正"替换/遮盖"语义、工具配对平衡校验、多次压缩合并
├── agent.py            # Agent.prompt()：每轮先 build_llm_payload() → 在给模型前触发 should_compact()
│     └── 需扩展：在 prompt() 开头调用 ContextManager.should_compact()，实现压缩后重建 payload
├── loop.py             # AgentLoop：接收 messages → 流式调模型 → 执行工具 → 回填
│     └── 需扩展：工具结果回填处调用 ToolExecutor 的截断/写盘逻辑
├── tool_executor.py    # 统一工具执行器
│     └── 需扩展：对 result.content 做 threshold/head/tail 截断（不破坏 tool_call_id 配对）
└── （新增）context_manager.py   # 推荐：ContextManager + TokenMeter，实现①触发/②压缩/③保留/④计量
```

**推荐的新模块接口（对齐 DSH 的 seam 思想）**：

```python
class TokenMeter:
    """计量器（可注入 estimate 函数）。"""
    def measure(self, session, context_window) -> ContextPressure
    def estimate_message(self, message_or_entry) -> int

class CompactionEngine(ABC):
    """压缩引擎抽象：定义 WHAT，不定义 HOW。"""
    @abstractmethod
    def compact_if_needed(self, session, context_window, signal=None) -> bool
    @abstractmethod
    def compact_now(self, session, signal=None) -> CompactionResult

class BasicCompactionEngine(CompactionEngine):
    """默认后端：触发判定 + 保留尾部 + 边界吸附 + 摘要调用 + 落地压缩。"""

class ToolResultPruner:
    """可选剪枝器：CompactionEngine 在压力时先调它，无模型重写超长工具结果。"""
```

这几层可以独立组合、独立测试，正是 DSH「service definition + provider + pruner + meter」的可替换 seam 思想（见 `01` §1）。

---

## 7. 重要警戒（容易被忽略的正确性点）

1. **切点工具配对平衡**：压缩区间切在未闭合工具调用上，会让模型历史缺配对而损坏。这是所有厂商都强调的（pi `findValidCutPoints` 绝不选 toolResult，Cline/Roo/OpenHands 都强制边界吸附）。
2. **恢复/中断语义**：若从磁盘恢复会话发现"assistant 有工具调用但无结果"或"请求已发出但无持久调用"，应合成状态告知模型"**只重试只读/幂等，验证副作用或问用户**"（DSH 的 `TOOL_OUTCOME_UNKNOWN`/`TOOL_NOT_STARTED`，见 `01` §10）。这对你的中断恢复模块极有价值。
3. **上下文占用只是展示，不作决策**：DSH 明确说占用率（`contextPressure`）是 UI 参考值，不是门控输入；压缩读的是 `measure()`。agent_lite 的 `token_count` 同理，应避免为了精确计量而过度复杂化。
4. **context rot**：Claude Code/Anthropic 证实"token 越多，召回越差"；社区实测在 ~50–65% 就显著退化。所以**不要等窗口快满才压缩**，`threshold_ratio 0.8` 相对安全，但你可测试更低的值是否让模型更聪明。
5. **don't hardcode 厂商数字**：把阈值做成配置项，针对你的模型校准。

---

## 8. 结语

agent_lite 的**数据层已经打好了很好的地基**（事件链 + compaction 数据模型 + 可注入 token 估算 + 持久化），你现在要补的是**"策略层"**：一个能回答"现在要不要管 / 怎么管 / 保留哪些"的 `ContextManager`，加上"工具输出管理"。

**推荐的最短路径**：先做阶段 A（计量 + 触发 + 溢出检测）→ 阶段 B（真正的 `/compact`，用非破坏 + 边界吸附 + 结构化摘要）→ 阶段 C（工具输出叠加头尾截断）。这三步做完，agent_lite 就已具备厂商级的上下文管理能力，且每一层都可独立测试、独立替换。

---

## 参考文档

- `01-deepseek-harness-context-management.md` —— DSH（本地源码）deep-dive，最完整的 seam 设计 + pi-ai 的估算/溢出实现
- `02-claude-code-context-management.md` —— Claude Code（官方 + 泄漏源码）8 大议题
- `03-codex-context-management.md` —— Codex（开源）9 大议题
- `04-other-coding-agents-context-management.md` —— 13 个 agent 横向对比 + 汇总表
- `README.md` —— 调研索引
