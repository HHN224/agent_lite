# 上下文管理调研 — 目录索引

> 目标：为 agent_lite 的**上下文管理模块**提供灵感和参考。**不改动现有代码**；调研结果只写入 `docs/research/*.md`。
> 考察对象：成熟 coding agent 厂商如何实现 agent 的上下文管理（触发时机、压缩机制、保留策略、token 计量、工具输出处理、KV cache 感知、系统/工具 prompt 组装、恢复/中断）。

## 调研文件

| 文件 | 覆盖对象 | 状态 |
|---|---|---|
| `01-deepseek-harness-context-management.md` | DeepSeek Harness（DSH，本地源码） | ✅ 已完成 |
| `02-claude-code-context-management.md` | Anthropic Claude Code（含泄露/逆向分析） | ✅ 已完成 |
| `03-codex-context-management.md` | OpenAI Codex（开源 codex-cli / agent-sdk / Responses API） | ✅ 已完成 |
| `04-other-coding-agents-context-management.md` | Copilot / Gemini CLI / Cursor / Aider / Cline / Roo / Continue / Goose / OpenHands / Devin / pi-agent / SWE-agent | ✅ 已完成 |
| `05-synthesis-and-recommendations.md` | 综合对比 + 给 agent_lite 的落地建议 | ✅ 已完成 |

## 调研维度（统一对比框架）

对每个供应商，统一记录以下维度，便于横向对比：

1. **触发时机**（Trigger）：多少 token / 多少窗口比例触发？压力阈值 vs 溢出确认？
2. **压缩机制**（Mechanism）：摘要到 system prompt？摘要成 user 消息？整体重写历史？保留什么？
3. **保留策略**（Retain recent）：保留最近的多少原文作为工作记忆（buffer / tail）？
4. **Token 计量**（Counting）：精确 tokenizer（tiktoken/cl100k）还是启发式（chars/4）？
5. **工具输出处理**（Tool output）：大工具结果在上下文里怎么处理（截断/省略/剪枝）？
6. **KV Cache 感知**（Prompt caching）：如何组织 prompt 以最大化前缀缓存命中？什么会失效缓存？
7. **系统/工具 prompt 组装**（Assembly）：system prompt 与工具 schema 的注入方式、顺序、变量插值。
8. **恢复/中断语义**（Recovery）：崩溃/中断恢复时，如何重建模型历史与工具状态。

## 核心结论（初步，随调研更新）

- **上下文管理 = 一次带锁的「surface 替换」（压缩）+ 独立计量器 + 可选剪枝器**，而不是一个巨型 ContextManager。
- 成熟厂商普遍采用：**事件溯源日志（append-only）+ 派生模型历史 + 压缩替换旧区间**。
- 默认起点通常：`thresholdRatio 0.8`、`retainRatio ~0.16`（保留 16% 窗口原文）、`maxTokens ~8192`。
- 压缩前**先无模型剪枝大工具输出**，可显著省钱、避免把大块输出喂进摘要。
- 摘要调用**回放对话前缀 + 追加固定指令**以复用 provider 前缀缓存（KV cache）。
- 压缩**切点必须工具配对平衡**（不能在未闭合工具调用的区间上切）。

---
*本索引及全部调研文件均为 research 产物，不涉及对 agent_lite 源码的修改。*
