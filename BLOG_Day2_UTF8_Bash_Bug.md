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



当我的 Agent 第一次遇到中文文件名：一次“假死”Bug 排查记录

日期：2026-08-21
项目：agent-lite —— 一个用 Python 复刻 pi agent 思路的 Coding Agent
阶段：Day 2 —— 第一次真实使用中暴露的问题

昨天我终于把自己的 Agent 打包成了一个可以全局运行的命令：

agent-lite

当时感觉已经挺完整了：

有模型调用
有工具系统
有 Agent Loop
有 Bash 执行能力
可以像 Coding Agent 一样帮我操作项目

结果今天第一次真正让它干活，就遇到了一个很奇怪的问题。

我问它：

“帮我看看当前目录有什么文件。”

这是一个再简单不过的任务。

正常情况下，它应该执行：

ls

然后告诉我：

当前目录有 xxx 文件。

但是实际情况：

它开始疯狂调用工具。

一次。

两次。

三次。

……

最后：

已达到最大工具调用次数。

看起来像“卡死”了。

先排除模型问题

第一反应：

是不是模型抽风了？

是不是 API 出问题？

但是查看 Agent 保存的会话记录后，我发现事情不是这样。

它并不是一直思考。

而是在不断重复：

执行命令
↓
工具报错
↓
模型尝试另一个命令
↓
继续报错

最多允许调用 10 次工具。

结果：

10 次全部浪费掉。

问题来了：

为什么模型一直重试？

因为它收到的错误是：

Error:
unsupported operand type(s) for +:
'NoneType' and 'str'

这个错误对模型来说基本等于：

“工具坏了，但不知道为什么。”

模型不知道是编码问题。

它只知道：

“刚才这个命令失败了，那换一个试试。”

于是：

ls
失败


find
失败


换一个 ls 写法
失败


python 脚本看看
失败

最后把次数耗光。

真正的问题在哪里？

最后我定位到了 Bash 工具：

result = subprocess.run(
    ...,
    capture_output=True,
    text=True
)


output = result.stdout + result.stderr

这里有一个隐藏炸弹：

text=True

它的意思是：

Python，你帮我把命令输出直接转换成字符串。

看起来很方便。

但是问题来了：

我的环境是：

Windows 中文系统
        ↓
Python
        ↓
Docker 容器

Docker 里面是 Linux。

Linux 默认使用：

UTF-8

比如一个中文文件：

实习计划

在计算机里面其实不是四个字。

而是一串数字：

e5 ae 9e ...

这些数字需要按照 UTF-8 规则才能还原成：

实习计划

但是 Windows 中文环境默认：

GBK

于是 Python 想：

我帮你解码一下。

结果：

Docker：

这是 UTF-8。

Python：

我按照 GBK 解。

两个人说的不是一种语言。

为什么有时候成功，有时候失败？

这里是最坑的地方。

如果所有中文都直接失败，反而容易发现。

但是实际情况：

有些命令正常。

有些命令炸。

原因是：

GBK 和 UTF-8 有时候会“误打误撞”。

比如：

某些 UTF-8 字节组合，GBK 也能勉强解释。

于是：

实习计划

可能显示成乱码：

瀹炰範璁″垝

但程序还能继续运行。

然而遇到某些特殊字节：

GBK 完全无法解释。

于是：

直接崩。

更诡异的是：错误没有告诉我编码问题

正常想象：

如果编码失败：

应该看到：

UnicodeDecodeError
UTF-8
GBK

类似的信息。

但是实际看到：

NoneType + str

完全看不出来。

为什么？

因为 Python 内部处理 subprocess 时：

它会在后台线程读取输出。

流程大概：

Docker输出
      |
      ↓
Python读取
      |
      ↓
尝试GBK解码
      |
      ↓
编码失败
      |
      ↓
读取线程异常退出
      |
      ↓
stdout变成None
      |
      ↓
我的代码:
None + "错误信息"
      |
      ↓
爆炸

所以真正的问题：

不是：

stdout为空

而是：

stdout解析过程中已经死了

最后留下一个完全误导人的错误。

修复方式

后来我把工具层改了一下。

以前：

subprocess.run(
    ...,
    text=True
)

让 Python 自动处理编码。

改成：

subprocess.run(
    ...,
)

先拿最原始的数据。

然后自己处理：

stdout.decode(
    "utf-8",
    errors="replace"
)

意思：

我明确告诉 Python，这个东西来自 Docker，用 UTF-8 解析。

如果真的遇到奇怪字符：

不要让程序崩。

用：

�

替代。

修改之后：

以前：

Docker UTF-8
      ↓
Windows GBK猜测
      ↓
可能爆炸

现在：

Docker UTF-8
      ↓
明确 UTF-8 解码
      ↓
正常字符串
另外发现一个小坑

修完工具后，我又发现：

如果终端本身还是 GBK。

那么：

工具输出：

�

打印到 Windows CMD：

也可能再次报错。

所以 CLI 层也增加了一层保护：

简单来说：

工具永远不能因为输出内容奇怪而让整个 Agent 死掉。

这次 Bug 给我的几个经验

虽然只是一个中文文件名导致的问题，但是它暴露了 Agent 开发里面几个很重要的问题。

1. Agent 最怕的不是模型错，而是工具错

模型其实一直在努力完成任务。

真正的问题是：

工具给了它错误的信息。

如果工具层返回：

执行失败
原因未知

模型只能盲猜。

2. 跨系统通信不要相信默认编码

以后：

只要涉及：

Windows
Linux
Docker
Shell
文件系统
网络传输

都不要依赖：

默认编码

应该：

明确指定。

3. Agent 的错误信息也需要设计

这次模型疯狂重试，有一部分原因是：

错误信息太差。

如果返回：

Bash执行失败


原因:
宿主编码解析失败


建议:
检查UTF-8输出

模型可能马上知道下一步怎么办。

总结

这次问题表面上：

Agent 卡死了。

实际上：

Agent 没有卡死，只是工具层因为编码问题一直失败，模型不断尝试修复一个不存在的命令问题。

一个看似简单的：

“查看目录文件”

背后涉及：

Windows 编码
Linux 编码
Docker
Python subprocess
Agent Loop
错误处理

这也是自己做 Agent 和调用 API Demo 最大的区别：

真正困难的地方，往往不是让模型回答问题。

而是让模型稳定地和现实世界交互。