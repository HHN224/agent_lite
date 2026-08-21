# 当 docker 的 UTF-8 撞上 Windows 的 GBK：一次"卡死"的 Agent 实战排查

> 日期：2026-08-21
> 项目：agent lite —— 用 Python 复刻自己的 pi agent 类物
> 阶段：Day 2 —— 真实使用中暴露出的诡异 bug 排查与修复
> 方向：自底向上分层（ai → agent_core → coding_agent），底层不能调用上层

## 一、背景

昨天把 Agent 打包成了全局命令 `agent-lite`，自认为很稳。今天用户实际跑起来，问了一句最普通的「帮我看看你这个目录下都有些什么东西」，结果 Agent 的表现是：**一连串工具调用后直接"卡死"，始终不给最终回答**。

第一反应是模型或 API 出问题，但翻开会话存档 `sessions/default.json`，真相立刻浮出水面——这不是无限死循环，而是 **10 轮工具调用（`max_iterations` 上限）被白白烧光**，循环以"已达到最大工具调用轮数"告终。用户感知的"卡死"，其实是十几秒的无效空转加上一句没头没尾的结束语。

为什么模型会像无头苍蝇一样反复重试？因为每轮它都收到同一个没头没脑的报错：

```
Error: unsupported operand type(s) for +: 'NoneType' and 'str'
```

模型看不懂这句话，只好换一个命令再试一次，然后再次收到一模一样的报错，直到轮数耗尽。

## 二、现场与现象

会话记录里的 bash 调用值得逐条看：

| 命令 | 结果 |
| --- | --- |
| `ls -la /workspace` | 正常（列出了含中文名的目录） |
| `find . ... \| sort` | ❌ `NoneType + str` |
| `ls -la <各子目录>` | 正常 |
| `for d in */; do ls "$d"; done` | ❌ `NoneType + str` |
| `ls -la && cat README` | 正常 |
| `python3 -c "...多行..."` | ❌ `NoneType + str` |
| `python3 -c "print(os.listdir('.'))"` | 正常 |
| `python3 -c "gbk 解码..."` | 容器内 `UnicodeEncodeError`（这是容器自己的报错，raw 透传） |
| `python3 << EOF` | ❌ `NoneType + str` |
| `write /tmp/inspect.py` | `PermissionError`（safe_path 拒绝越界） |

关键信息有两条：
1. **成功 / 失败的边界很诡异**：有些含中文输出的命令成功，有些失败，看不出简单规律。
2. 报错带 `Error: ` 前缀——这是 `AgentLoop._execute` 的 `except Exception` 兜底格式，说明异常是**宿主 Python 侧**抛出来的，而不是容器内的命令报错（容器内报错会像第 8 行那样 raw 透传）。

既然"宿主侧抛异常"，那最可疑的就是 `BashTool.execute` 里的这一行：

```python
result = subprocess.run(..., capture_output=True, text=True, timeout=...)
output = result.stdout + result.stderr   # ← 崩溃点
```

`result.stdout` 怎么会是 `None`？`capture_output=True, text=True` 按理说给的一定是字符串。

## 三、根因：UTF-8 撞上 GBK（最有价值的部分）

用最小复现把它钉死。这台开发机是中文 Windows，Python 的 `locale.getpreferredencoding()` 返回 `cp936`（GBK）。让一个子进程输出**宿主 GBK 解码不了的字节**，再用 `text=True` 捕获：

```python
subprocess.run(['python', '-c', "sys.stdout.buffer.write(b'\\xe7\\x20abc')"],
               capture_output=True, text=True)
```

结果让人大开眼界：

```
Exception in thread Thread-1 (_readerthread):
UnicodeDecodeError: 'gbk' codec can't decode byte 0xe7 ...
>>> result.stdout
None        # ← 竟然真的是 None！
```

**机制链条（Python 3.12 专有）：**

1. `text=True` 时，子进程输出的字节解码发生在后台 **`_readerthread`** 线程里（3.12 起才改为线程内解码）。
2. docker 容器是 `python:3.12-slim`，locale 是 C/POSIX，中文文件名以 **UTF-8 字节**原样输出。
3. 宿主用 **GBK** 去解这些 UTF-8 字节——大多数中文字能"贪心解码"成乱码（所以 `ls -la` 显示出一堆 `瀹炰範璁″垝` 之类的 mojibake 还能成功），但一旦碰到 GBK 里**不合法的字节组合**，`_readerthread` 直接抛 `UnicodeDecodeError` 死掉。
4. reader 线程一死，`communicate()` 收不到完整输出，`subprocess.run` 返回的 `CompletedProcess.stdout` 就成了 **`None`**——而不是干净地往上抛一个 `UnicodeDecodeError`！
5. 于是 `result.stdout + result.stderr` 变成 `None + str` → `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`，被 loop 兜底后回填给模型。

一句话总结：**解码错误发生在后台线程里，被 Python 静默"吃"掉，只留下一个 `None`，而 None 在拼接时才爆炸，报错指向了完全无关的地方。** 这正是它难排查的原因——异常信息里没有任何"编码"、"UTF-8"、"GBK"字样。

## 四、修复与回归

### 核心修复：字节捕获 + 显式 UTF-8 解码

`BashTool.execute` 去掉 `text=True`，改按字节捕获，再用 `errors="replace"` 容错解码：

```python
result = subprocess.run([...], capture_output=True, timeout=self.timeout)
out = (result.stdout or b"").decode("utf-8", errors="replace")
err = (result.stderr or b"").decode("utf-8", errors="replace")
output = out + err
```

三个要点：
- **字节模式下 reader 线程只搬运字节，永远不会因解码而死** → `stdout` 不再可能是 `None`，`None + str` 从根上消失。
- **按 UTF-8 解码**：docker 输出本来就是 UTF-8，中文文件名从此正确显示（`实习计划` 而不是 `瀹炰範璁″垝`）。
- **`errors="replace"`**：万一有真坏字节，用 `�` 兜底，工具永远不崩溃。

`or b""` 顺手把历史遗留的 `None` 场景也兜住了——防御性编码的典型案例：就算上游哪天又给个 `None`，这里也不炸。

### 加固：CLI 打印的第二个编码坑

修完工具层，发现还有个同源隐患：工具内容若含 `�` 替换符，`cli_listener` 里的 `print()` 到 GBK 终端会再抛一次 `UnicodeEncodeError`，导致整轮被跳过。加了 `safe_print()`，打印时用 `errors="replace"` 重编码兜底，把"内容不可打印"也挡在 REPL 之外。

### 回归测试：不依赖真实 docker

单元测试里 monkeypatch `subprocess.run`，用伪造的 `CompletedProcess` 覆盖四种情况：
- `stdout=None`（复刻历史崩溃）→ 不抛异常、保留 stderr
- UTF-8 中文输出 → 正确解码出「实习计划」
- GBK 不可解码的坏字节 → 不崩溃、内容保留
- 无输出 → 回填 exit code

`pytest` 全量 **34 passed**，另用真实 docker 冒烟（`echo hello-世界 && ls`）确认普通路径不受影响。

## 五、当前的位置与下一步

这次的收获不只是修好了一个 bug，而是给项目的编码约定定了个规矩：**凡是跨进程/跨容器拿文本，一律按字节捕获 + 显式 `utf-8` 解码，绝不把解码交给宿主 locale 默认值**。中文 Windows 的 GBK 默认值 + 容器的 UTF-8，是一对天然的地雷。

下一步方向：
- **上下文压缩**：超长对话自动总结（pi 的 compaction 设计），这个 bug 暴露了"对话历史里塞满失败工具结果"会很快耗尽轮数与上下文
- **工具错误信息工程**：这次模型反复重试，一半原因是被无意义报错带偏——思考如何让工具错误信息更"可行动"
- **审批门**：`dangerous=True` 字段的消费者（bash 这类危险工具需用户确认）
