# 当一个"有锚点就够"的上下文计量，悄悄漏掉了增长：一次自己发现问题、和 AI 对答案的排查

> 日期：2026-08-22
> 项目：agent lite —— 用 Python 复刻自己的 pi agent 类物
> 阶段：Day 3 —— 上下文管理（阶段 A：计量 + 触发）
> 方向：自底向上分层（ai → agent_core → coding_agent），底层不能调用上层

## 一、背景

昨天修完编码 bug 后，我开始做上下文管理。计划里很清楚：计量要用「启发式 + provider usage 锚点」——优先复用 provider 上报的真实 usage 作锚点，拿不到就退回 `chars/4` 启发式。这是 DeepSeek Harness / pi / Claude Code 共同的做法，我没多想就照做了。

写出来的逻辑很直白：`measure()` 只要拿到了 usage，就直接返回它：

```python
if usage is not None:
    total = usage_total(usage)
    if total is not None:
        return ContextPressure(total_tokens=total, ...)   # ← 有锚点就用锚点
```

## 二、我自己的疑问：这测量的是"过去的上下文"

写完代码跑通后，我盯着 `context_check` 那行输出反复看，越看越觉得不对劲。我当时的原话是：

> 我看了下这个程序，测量好像是直接测量**过去对话**的 token 量，锚点也是锚到**当前会话的最末端**。那是不是意味着，现在**没办法**做到：对话进行中，拿到"最近一次的真实 usage"+"当前新增内容的启发式估算"，如果**总和**到了上限，就先中途压缩一下再继续对话？

这个直觉最后被证明是对的，而且指向一个真正的 bug。

## 三、问题拆解：锚点不是"会话末尾"，是"上一次调用"

关键在 `last_usage` 的语义。它不是"当前会话有多长"，而是**上一次成功调用模型时，模型看到的 prompt 有多长**。

于是一条时间线就说不通了：

```
第 1 次调用前，prompt = 70000    ← usage 锚点记录的是这一刻
  ↓ （模型回复、工具结果、新消息陆续 append 进会话）
第 2 次调用前，session 里已经又多了 15000 token
  ↓
但我的代码在读锚点时，拿到的还是 70000
```

我的 `measure()` 看到锚点=70000，直接用它当"当前占用"。于是：
- 窗口 100000，阈值 80%（=80000）
- 锚点 70000 → 占比 70% < 80% → **判定"不压缩"**
- 但实际会话已经长到 85000（85% > 80%）→ **应该压缩**

这就是漏检：**锚点永远等于"上一次调用时的上下文"，它从不等同于"当前上下文"；而它之后的增长，没人补上。** 结果就是只要锚点低于阈值，哪怕会话已经远超阈值，也一直报"正常"，永远不会触发压缩。

## 四、和 AI 对答案

我把上面的推理想法讲给 AI，确认了两件事：

1. **结构上这个流程能实现**——每轮 `prompt()` 开头本来就调了一次计量，发生在构建本轮 payload 之前。"检测 → 超限 → 先压缩 → 再继续"的接线已经在，只差压缩动作本身（那是阶段 B）。所以不是"做不了"。
2. **但计量确实是简化版**——"有锚点就用锚点，没锚点就全启发式"，确实**没有**做"锚点 + 新增内容"的相加。AI 给了个更直白的例子点破它：

> 锚点 70000（70%）还没到阈值，但这轮调用后又追加了 15000，实际已达 85000（85%），应该压缩了。代码直接返回 70000 判定不压缩——**漏检了**。

## 五、解决办法：锚点 + 增量

等式其实很朴素：

```
当前占用 ≈ 最近一次真实 usage（锚点） + 启发式估算（锚点之后新增的内容）
```

但要这么做有个前提：**得记住锚点对应到会话的哪条消息**，才知道"锚点之后新增了哪些"去估。所以我引入了 `UsageAnchor`，把"锚点值"和"边界"绑在一起：

```python
@dataclass(frozen=True)
class UsageAnchor:
    total_tokens: int    # 上次真实 usage 总量
    head_id: str | None  # 当时会话推进到的 head 条目，用来算增量从哪起
```

`measure()` 改成：

```python
if anchor is not None:
    base = anchor.total_tokens
    delta = estimate_entries_after(session, anchor.head_id)   # 锚点之后新增的 message
    total = base + delta + pending_total                       # 再补上本轮还没入历史的消息
else:
    total = estimate_payload(session.build_llm_payload())      # 没锚点，整体启发式
```

`ContextPressure` 上也暴露了 `anchor_total`（锚点基准）和 `incremental`（增量），以后 UI 可以直接看到"锚点占多少、新增占多少"。

## 六、验证

给这段逻辑补了一组用例，最关键的是复现漏检场景的一个：

```python
anchor = UsageAnchor(total_tokens=4000, head_id=e_anchor.id)  # 4000/10000 = 40% < 50%
s.append_message("assistant", "x" * 4000)                      # 锚点后新增大量内容
assert mgr.should_compact(s, context_window=10000, anchor=anchor) is True  # 靠增量才越线
```

锚点 40% 明明低于阈值，但因为锚点后的新增把实际占用推过了线，现在能正确判"压缩"了。边界情况也覆盖了：锚点边界不在当前链上时保守记 0，避免重复计数；增量只统计 message 条目不重复计 compaction。全量 **117 passed**。

## 七、当前的位置与下一步

这次的收获，是给"计量"补上了一个很容易被忽略的语义：**锚点是"上一次调用时刻"的快照，不是"当前"的快照，中间的差值必须用增量补上**。这直接关系到压缩能不能被正确触发——计量错了，后面所有的"该不该压"都会跟着错。

下一步方向：
- **阶段 B（真正的压缩）**：边界吸附 + 保留尾部 + 独立摘要调用，把 `should_compact` 变成真的 `compact_now`
- **阶段 C（工具输出剪枝）**：大工具结果做 head + marker + tail 叠加，最省 token 的一步
- **溢出强制恢复 + 熔断**：provider 真报 `context_window_exceeded` 时强制压一次，且连续失败后本会话跳过自动压缩
