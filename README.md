<div align="center">

# Agent Lite

用 Python 实现的交互式 Coding Agent，探索工具调用、会话状态与长对话上下文管理。

[![Version](https://img.shields.io/badge/version-0.1.0-blue?style=for-the-badge)](pyproject.toml)
[![Stars](https://img.shields.io/github/stars/HHN224/agent_lite?style=for-the-badge)](https://github.com/HHN224/agent_lite/stargazers)

</div>

Agent Lite 是一个受 pi-agent 启发的学习与实验项目。它通过 OpenAI 兼容接口连接模型，在终端中读取、搜索和修改文件、执行命令，并保存会话；当前实现包含独立的模型适配层、Agent 核心和 CLI / TUI 应用层。

## 当前能力

- **工具调用循环**：流式接收模型回复，执行工具并回填结果；包含参数校验、权限确认、超时处理、审计记录和循环次数限制。
- **七个内置工具**：`read`、`write`、`edit`、`bash`、`grep`、`ls`、`find`。
- **两种终端界面**：行式 CLI，以及基于 Textual 的 TUI，后者提供 Markdown 消息卡片、可折叠工具结果和运行中插话。
- **会话持久化**：自动保存、恢复最近会话、按 ID 恢复，以及新建、命名、列出和删除会话。
- **上下文管理**：结合 API 输入 token 用量与新增内容估算，在阈值处生成摘要并保留近期原文；超长工具输出截断后保存全文。
- **受控取件**：沙箱不联网，装依赖与取文件走域名白名单通道，不执行被下载代码，全程审计。
- **页面验证**：`render_page` 用本机 headless Edge/Chrome 跑页面，回报运行时错误与交互是否生效，替代"开浏览器抄堆栈"的人工回路。
- **任务模式与图片输入**：提供 `default` / `plan` / `code` / `review` 系统提示词；TUI 可通过 `/image` 发送本地图片，是否可识别取决于所选模型。
- **可替换命令后端**：Docker、WSL、宿主机执行，以及自动探测。

## 快速开始

### 1. 安装

建议使用 **Python 3.12+**。打包配置目前声明 Python 3.10+，但 TUI 源码使用了 Python 3.12 才支持的 f-string 语法；当前入口会尝试导入 TUI，使用 3.12+ 可避免这一兼容问题。

```bash
git clone https://github.com/HHN224/agent_lite.git
cd agent_lite
python -m venv .venv
```

激活虚拟环境：

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

安装运行依赖和可选 TUI：

```bash
python -m pip install -e ".[tui]"
```

只需要 CLI 时可安装 `python -m pip install -e .`。未安装 Textual 时，默认界面会回退到 CLI。Docker 并非必装依赖，仅 Docker 后端需要它。

### 2. 配置模型密钥

复制仓库根目录的 `.env.example` 为 `.env`，填写 `CMD_API_KEY`。程序会加载项目根目录的 `.env`，并尝试加载当前目录的环境配置；已设置的环境变量不会被覆盖。

`.env` 和 `sessions/` 已加入 `.gitignore`。默认使用 Command Code 和 `CMD_API_KEY`，不需要 DeepSeek 官方密钥。使用 `--provider deepseek` 时读取 `DEEPSEEK_API_KEY`。

**Command Code GOAT 套餐：** 在 [Studio](https://commandcode.ai/settings/keys) 创建 API key，将 `CMD_API_KEY=你的密钥` 加到本地 `.env`，然后运行：

```powershell
agent-lite --bypass --sandbox wsl
```

默认端点为 `https://api.commandcode.ai/provider/v1`，模型为 `deepseek/deepseek-v4.1-flash`。官方说明 [GOAT 支持 API 调用并计入套餐额度](https://commandcode.ai/docs/plans/goat#api-support)；模型仍受套餐可用范围限制。本适配器使用 [Chat Completions 协议](https://commandcode.ai/docs/provider)，支持文本、图片、思考流、工具调用和 usage；Claude 的 Anthropic Messages 协议暂未接入。对话和上下文压缩使用同一个 provider，不会回退到 DeepSeek 官方 API。

### 3. 启动

先使用 CLI 体验文件读取和权限确认：

```bash
agent-lite --ui cli
```

在 `>` 后输入任务，例如：

```text
读取 README.md，列出这个项目的主要模块。
```

安装了 Textual 后，直接执行 `agent-lite` 即可进入默认 TUI。也可以使用 `python -m coding_agent` 启动。

程序默认恢复最近会话。要新开一个有名称的会话：

```bash
agent-lite --ui cli --new --name "代码阅读" --workspace .
```

启动时会显示会话 ID、存档位置、工作目录和实际命令后端。默认通过 Command Code 使用 `deepseek/deepseek-v4.1-flash`。对话和上下文压缩摘要使用同一 provider 和模型，也可使用 `--model` 指定其他模型。

## 界面与会话命令

| 命令 / 操作 | CLI | TUI |
| --- | --- | --- |
| `/new` | 新建会话，沿用当前任务模式 | 同左 |
| `/clear` | 清空当前历史并保存 | 同左 |
| `/mode default`（也可选 `plan` / `code` / `review`） | 切换系统提示词 | 同左 |
| `/sessions` | 列出会话及 ID | 同左 |
| `/resume 会话ID` / `/older` | 未提供 | 切换会话 / 加载更早记录 |
| `/bypass` / `/bypass off` | 使用启动参数 `--bypass` | 自动通过工具请求 / 恢复原权限策略 |
| `PgUp` / `PgDn` / `Ctrl+End` | 使用终端滚动 | 翻页 / 恢复跟随最新输出；滚轮上翻暂停跟随 |
| `/compact` | 手动生成上下文摘要 | 同左 |
| `/deps` / `/deps clear` | 查看受控取件的依赖与审计日志 / 清空依赖 | 未提供 |
| `/name 显示名` | 重命名当前会话 | 未提供 |
| `/delete 会话ID` | 确认后删除非当前会话 | 未提供 |
| `/help` | 未提供；启动参数见 `--help` | 显示界面命令 |
| `/image 本地图片路径` | 未提供交互命令 | 读取图片并发送给模型 |
| 运行中发送补充说明 | 等待本轮结束后输入 | 加入插话队列，在循环检查点交给模型 |
| `Ctrl+C` | 执行中中止本轮；等待输入时退出 | 执行中请求停止；空闲时清除草稿或退出 |
| `Esc` / `Ctrl+D` | — | 请求停止 / 退出 |

任务模式只改变提示词，**不会改变工具权限**。例如 `plan` 不是强制只读模式；需要拒绝写入和命令执行时，请使用 `--permission-policy deny`。

TUI 显示模型思考文本，并在状态栏标记当前阶段、耗时和调用轮数。任务没有最大轮数限制；完成、用户停止或不可恢复错误时结束。自动上下文压缩也会在工具轮次之间检查。Esc 在下一安全检查点停止，无法强制打断已进入的同步网络读取或外部工具。恢复会话时也显示已保存的思考文本。

Windows 输入解析会区分终端能力回复与用户按键，并处理跨批次到达的控制序列。shell 工具为非交互命令，标准输入关闭，Windows 子进程不共享 TUI 控制台。`write` 自动创建工作目录内缺失的父目录。图片工具结果在发送 API 时转换成配套的用户图片消息，保留工具调用配对和原始会话存档。

会话保存于项目根目录的 `sessions/<id>.json`，默认恢复最近更新的会话。`--session` 接受已有 ID（1–64 位字母或数字），不是展示名，且优先于 `--new`；可先用 `/sessions` 查看 ID，再重新启动恢复。

## 启动参数

完整帮助：`agent-lite --help`。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--ui` | `tui` | `tui` 或 `cli`；缺少 Textual 时回退 CLI |
| `--provider` | `commandcode` | `commandcode`（GOAT）/ `deepseek` |
| `--workspace` | 当前目录 | 文件工具的默认根目录和命令工作目录 |
| `--session` | 空 | 恢复指定会话 ID；也可由进程环境变量 `AGENT_SESSION` 指定 |
| `--new` | 关闭 | 不恢复最近历史，新建会话 |
| `--name` | 空 | 新建会话的展示名 |
| `--mode` | `default` | 新建会话使用的任务模式；恢复会话保留已存提示词 |
| `--model` | 随 provider 选择 | DeepSeek: `deepseek-flash`；Command Code: `deepseek/deepseek-v4.1-flash` |
| `--base-url` | 随 provider 选择 | 可覆盖 OpenAI 兼容接口地址 |
| `--permission-policy` | `ask` | `ask` 确认 / `deny` 拒绝 / `auto` 放行危险工具 |
| `--bypass` | 关闭 | 等同 `--permission-policy auto` |
| `--sandbox` | `auto` | `auto` / `docker` / `wsl` / `host` |
| `--bash-image` | `python:3.12-slim` | Docker 后端所用镜像 |
| `--allow-host` | 空（可重复） | 扩展受控取件的域名白名单，例如 `--allow-host example.com`；不改沙箱本身（沙箱始终无网络出网），`render_page` 也用这份名单放行页面请求 |
| `--no-browser-image` | 关闭 | `render_page` 不把截图附加给模型（只给路径），用于模型不支持图片输入时 |
| `--context-window` | `128000` | 上下文计量窗口；设为 `0` 关闭自动上下文检查 |
| `--compact-threshold` | `0.8` | 自动压缩触发占比 |
| `--compact-retain` | `0.16` | 压缩时尾部原文的目标预算，占窗口的比例 |
| `--compact-max-tokens` | `2000` | 摘要提示词中的长度目标；当前不是 API 硬性输出上限 |

## 工具与执行边界

| 工具 | 用途 |
| --- | --- |
| `read` | 读取带行号的文本，可指定 `offset` / `limit` / `max_chars`（默认 2000 行、6000 字符）；读不完时结果自带「已显示第 A–B 行，共 N 行」与下一段 `offset`；图片转为多模态内容 |
| `write` | 写入 UTF-8 文本 |
| `edit` | 精确替换唯一匹配的字符串；不存在或多处匹配时返回错误 |
| `bash` | 通过选定后端执行命令，返回退出码、标准输出和错误输出 |
| `grep` | 搜索文件内容，支持正则、字面量和文件过滤 |
| `ls` | 列出目录内容 |
| `find` | 按名称及其他条件查找文件或目录 |
| `install` | 受控取件：把 Python 依赖下到工作区并解包到 `.agent-lite/deps/python`（已在沙箱 `PYTHONPATH` 上） |
| `fetch` | 受控取件：把白名单域名下的一个 http(s) URL 下到 `.agent-lite/downloads/` |
| `render_page` | 在本机浏览器（headless Edge/Chrome）里真的跑一遍工作区内的页面，回报运行时错误、被拦请求、canvas 尺寸、首屏文本与交互是否改变画面 |

`write`、`edit`、`bash` 标记为危险工具，受权限策略控制。默认 `ask` 使用终端 `y/N` 确认；当前 TUI 也沿用该确认函数，尚无专用审批弹窗，需要逐次确认时建议使用 CLI。

`install` 与 `fetch` **不是**危险工具，也不需要逐条确认：它们的安全性由机制保证，而不是由用户在弹窗上点 yes（审批疲劳下的 yes 等于没有确认）。

## 页面验证：`render_page` 为什么必须有

前端 / 可视化（WebGL、canvas、交互）类任务里，**静态推理补不上运行时反馈**。这不是推测，是真实 session 的取证结论：agent 反复确认"no browser available，只能靠仔细推理"，于是**人被迫当测试台** —— 手动开 Edge、把控制台堆栈抄进 `sessions/scene_browser_feedback*.txt` 再粘回来，3 轮往返只修掉 3 个运行时错误（着色器编译失败、`z is not defined`、方法名写错）；同一批 session 还花了 12 条 bash 命令去 `which chromium` / `import playwright` 找浏览器。

`render_page(path, wait_ms, actions, screenshot)` 把这条回路交还给 agent：

- **复用你已装的 Edge / Chrome**（`channel: msedge → chrome → bundled`），不下载 Chromium；Node 从 PATH 取，Playwright 模块自动探测（也可用 `AGENT_LITE_PLAYWRIGHT` 指定）。
- **文本优先**：捕获 `pageerror`、`console.error`（**WebGL / 着色器编译错误就在这里**）、`console.warning`、被拦请求、页面标题、canvas 尺寸、首屏可见文本。
- **交互验证**：给出 `actions`（`drag` / `wheel` / `click` / `wait`）后做**像素差分**，把"渲染循环与交互到底活着没有"变成一个明确的「发生了变化 / 没有任何变化」。
- **页面通过网络访问外部**：在浏览器层拦截，只放行 `127.0.0.1` 与白名单域名，其余 abort 并记入报告与 `.agent-lite/network.log`。
- **静态服务只绑 `127.0.0.1`，且拒绝服务 `.env`、`*.pem`、`sessions/`、`.git/`、`.agent-lite/`**（实测页面内 `fetch('/.env')` 得到 403）。没有这层，页面同源一句 fetch 就能读到密钥与全部对话存档。
- **截图**落在 `.agent-lite/browser/`（全尺寸 png + 小 jpg + 交互后 png）；只有 ≤150KB 的小图才会附加给模型（`--no-browser-image` 可关）。这条限制来自实测：模型确实吃图（user 消息里放图能答对颜色），但 tool 结果里 205KB 的图片曾触发过 HTTP 400。

诚实边界：它能自动化**错误回路**，不能判断"好不好看"（审美仍需你打开截图看）；headless 与真实浏览器在字体/GPU 上仍有差异；交互自动化本身可能 flaky（默认只做少量动作、每个动作后等待稳定）。

依赖不可用时（没有 node，或 node 加载不到 playwright），工具会**直接说清缺什么、怎么补**，而不是静默失败：

```powershell
npm i -g playwright
npx playwright install msedge   # 或者直接用已装好的 Edge（channel: msedge）
```

## 联网策略：沙箱不联网，取件走受控通道

沙箱（WSL / Docker 档）**始终没有网络出网能力**，这不是省事，而是刻意选择：

- 工作区里通常躺着密钥与会话存档，任何一条出网路径都可能被注入的指令用来带走它们；而逐条确认挡不住这个。
- 沙箱里没有 pip/npm，`/tmp`、`/home` 又是每条命令丢弃的 tmpfs：**就算开了网，也装不上、装上留不住**。所以「用不了某个库」的病根是「取件 + 落地」两件事，不是网络一件事。

于是取件被收敛成两个**在沙箱外执行、不执行被下载代码**的窄通道：

1. **机制强制**：域名白名单（默认 pypi / files.pythonhosted / registry.npmjs / github 等，`--allow-host` 可扩展）；解析后校验 IP，拒绝私网、回环、链路本地地址（阻断 SSRF 与内网横向）；重定向每一跳都重新校验；响应与依赖总量都有上限。
2. **不执行代码**：`pip download --only-binary=:all:` 只取 wheel（绝不下 sdist，sdist 的 `setup.py` 会被执行），并做 Linux x86_64 跨平台下载（Windows 侧装 win_amd64 wheel 到 WSL 是白下）；wheel 就是 zip，解包即可用，所以**沙箱里没有 pip 也能装**。
3. **归档防护**：解包时挡 zip-slip 与软链接，跳过 `.data/`；并拒绝会让顶层模块遮蔽 Python 标准库的 wheel —— `PYTHONPATH` 优先于标准库，一个 `json.py` 就能顶替标准模块。
4. **落地位置固定**：`.agent-lite/deps/python`（已 gitignore），沙箱命令自动带上 `PYTHONPATH=/workspace/.agent-lite/deps/python`，跨命令持久。
5. **事后可查**：每次取件 append-only 写进 `.agent-lite/network.log`，`/deps` 可随时查看依赖、体积、白名单与最近记录，`/deps clear` 一键回到干净状态。

同时，沙箱内**内核级掩蔽**敏感内容：`.env`、`*.pem`、`*.key`、`id_rsa`、`.netrc`、`.npmrc` 等文件用 `/dev/null` 覆盖（实测读取为 `Permission denied`），`.ssh`、`.aws`、`.gnupg`、`secrets` 以及 agent 自己的 `sessions/` 目录用空 tmpfs 覆盖。这一层与网络策略互补：即便将来某条路径漏了，能被带走的私有数据也少了一大半。**host 档没有内核隔离能力，掩蔽不生效**，启动时会如实打印。

诚实边界：pip 自身的下载目标由其配置（`pip.conf` / 镜像）决定，白名单的强制作用在 `fetch` 工具与内网阻断上；`install` 会把 pip 实际使用的索引（`Looking in indexes`）写进结果，方便核对。私网阻断与请求之间仍有 TOCTOU 窗口，不是完整的 DNS rebinding 防御。若将来确实需要「沙箱内任意程序真联网 + 域名级强制白名单」，那需要 Docker 代理 sidecar，或 WSL 内 netns + veth + iptables 的一次性 root 安装 —— 成本明显更高，暂不做。

`read` 的单次字符预算（默认 6000）低于工具输出的截断阈值（默认 8192），所以「只读到一部分」由 `read` 自己带着行号区间说清楚，而不会被上层从字符中间一刀切掉、毁掉行号结构。写盘的工具全文（`.agent-lite/tool-results/<会话 id>/`）就是普通文本文件，`read` 配 `offset` / `limit` 可分段读完。

`--sandbox auto` 按 **Docker → WSL → host** 探测，最终可能落到宿主机执行。需要指定后端时显式设置参数；指定 Docker / WSL 后若探测不可用，启动会报错，不自动降级。

| 后端 | 实际行为 |
| --- | --- |
| `docker` | 一次性容器，禁用网络、只读根文件系统、512 MB 内存和 100 进程限额；工作目录可写挂载到 `/workspace`，`/tmp` 可写 |
| `wsl` | 在默认 WSL 发行版中通过 Bubblewrap 执行；无网络、系统只读，仅工作目录可写 |
| `host` | 在宿主机通过 `shell=True` 执行；仅设置工作目录，没有文件系统或网络隔离，也不保证使用 Bash；**密钥掩蔽在此档不生效** |

工具说明书里给模型的是**宿主真实工作目录的绝对路径**，而不是容器内的 `/workspace`：后者只是 bash 侧的别名，`read` / `write` / `edit` 并不认识它，直接写 `/workspace/...` 会被 `safe_path` 拒绝。bash 的描述里会同时点明两者的关系（真实路径 + 仅限 bash 的容器别名），避免模型把别名当成自己的工作目录。

WSL / Docker 档还会在挂载层掩蔽敏感内容（见「联网策略」一节）：`.env` 等文件读作 `Permission denied`，`sessions/` 等目录呈现为空目录；被掩蔽路径是**故意**的，工具说明书里也这么告诉模型，避免它把 `Permission denied` 误当成故障去绕。

使用 Docker 前启动 Docker 服务并准备镜像：

```bash
docker pull python:3.12-slim
agent-lite --ui cli --sandbox docker
```

使用 WSL 后端前，准备一个普通 Linux 发行版并安装 Bubblewrap（不要使用 Docker Desktop 的内部发行版）：

```powershell
wsl --install -d Ubuntu-24.04
wsl --set-default Ubuntu-24.04
```

```bash
sudo apt update
sudo apt install bubblewrap
sudo mkdir -p /workspace
```

建议在该发行版的 `/etc/wsl.conf` 中关闭 Windows 程序互操作；保留自动挂载，以便 Bubblewrap 只把当前工作目录映射到 `/workspace`：

```ini
[automount]
enabled=true

[interop]
enabled=false
appendWindowsPath=false
```

修改后在 PowerShell 运行 `wsl --shutdown`，再使用：

```bash
agent-lite --ui cli --sandbox wsl
```

`read` / `write` / `edit` 使用 `safe_path` 限制路径，但当前搜索、目录工具与 TUI `/image` 并未统一采用这一检查。因此 `--workspace` 不能视作整个程序的安全沙箱，Docker/WSL 的隔离也只覆盖 `bash` 命令执行。WSL 后端不提供 Docker 后端的内存和进程数限制。

## 上下文如何管理

1. **计量**：以服务端返回的 `prompt_tokens` 校准已有输入，用启发式方法估算新增内容；不是精确 tokenizer 计数。
2. **工具输出截断**：超过默认 8192 字符的文本结果生成预览，全文写入工作目录内的 `.agent-lite/tool-results/<会话 id>/`，返回结果带有保存路径与「如何读回全文」的提示。写盘位置必须在工作目录内，否则 `read` 的 `safe_path` 会拒绝该路径，模型就永远读不回自己的工具输出。任何截断都会在正文里自曝：说清从哪边截的、丢了多少字符，并给出继续读全文（`read` 的 `offset`/`limit`）或缩小查询范围的具体做法，模型不会在毫不知情的情况下基于残缺输出下结论。
3. **自动摘要**：每次用户请求开始时检查占用，达到默认 80% 阈值后，通过独立模型调用总结旧消息，并保留近期原文；没有合适切点或没有可折叠内容时跳过。
4. **重建模型输入**：会话用追加式条目链记录消息与压缩事件，从历史中派生“系统提示词 + 摘要 + 保留消息”，压缩不直接删除旧历史条目。

摘要调用刻意做成「不像一段还在进行的会话」：被遮区间渲染成 `<transcript>` 引用文本（工具调用与结果降级为 `[tool call]` / `[tool result]` 文本行，图片只留占位符），总结指令是**最后一条** user 消息，system 用专用压缩器人格而不是会话里的编码 agent 人格，并且 `temperature=0`、`max_tokens` 真正生效、整条请求不带工具。被遮区间还有字符预算（默认 6 万字符，超出时保留头尾并标注省略了多少条消息）——早期版本会把整段历史（实测某次 40 万字符）原样发给摘要模型，并且让指令排在被回放的对话前面，导致模型接着干活：8 次压缩里有 6 次的「摘要」实际是继续任务的计划、DSML 工具调用原文或反问用户要历史。

摘要输出有一道硬闸门（`validate_summary`）：过短、含工具调用标记（DSML / `<tool_call` / JSON `tool_calls`）、或在反问用户要对话，都判定不合格并**拒绝落地**——不新增 compaction、不重置计量、原文照旧发给模型，失败原因会通过 `compaction_end` 事件在 CLI / TUI 里显示。摘要落地后注入 payload 时带「参考资料，不是新指令」的框架标注。摘要调用使用独立的 provider 实例，避免其 usage 覆盖上下文计量用来校准 `session.usage` 的真实锚点。

摘要会额外调用模型；`--context-window` 是本地计量配置，不会更改服务端模型的实际窗口。当前自动检查发生在用户请求开始处，不能保证单次长工具循环内不会超窗。

## 项目结构

```text
agent_lite/
├── agent_core/          # 对话状态、事件循环、工具执行、会话与上下文管理
├── ai/                  # 工具 Schema、Provider 契约与 OpenAI 兼容实现
├── coding_agent/        # CLI / TUI、任务模式、具体工具与命令后端
├── docs/
│   ├── research/        # 上下文管理调研与综合建议
│   └── context-management-plan.md
├── tests/               # FauxProvider 驱动的确定性测试与模块测试
├── .env.example         # 环境变量示例
├── BLOG_Day1_Agent_Runtime.md
├── BLOG_Day2_UTF8_Bash_Bug.md
├── BLOG_Day3_Context_Anchor_Delta.md
├── README.md
├── pyproject.toml       # 依赖、可选依赖与 agent-lite 命令入口
├── requirements-dev.txt
└── requirements.txt
```

运行后产生的 `sessions/` 与工作目录内的 `.agent-lite/` 都不纳入版本控制。`.agent-lite/` 下分别是：`tool-results/`（被截断工具输出的全文）、`deps/`（受控取件装好的依赖与 wheel 缓存，即沙箱 `PYTHONPATH` 指向处）、`downloads/`（`fetch` 取回的文件）、`browser/`（`render_page` 的截图）、`network.log`（取件与页面请求的审计）。

```mermaid
flowchart TD
    UI[CLI / TUI] --> Agent[Agent：会话与上下文]
    Agent --> Session[Session / SessionRepository]
    Agent --> Context[ContextManager / CompactionEngine]
    Agent --> Loop[AgentLoop：模型与工具循环]
    Loop --> Provider[LLMProvider / OpenAIProvider]
    Loop --> Executor[ToolExecutor：校验、权限、执行]
    Executor --> Tools[文件与搜索工具 / BashTool]
    Tools --> Runner[CommandRunner：Docker / WSL / host]
```

模型协议封装在 `ai`，核心循环不依赖具体 CLI、TUI 或命令后端。应用入口负责组装 Provider、工具、会话和上下文策略；新工具实现 `AgentTool`，新命令后端实现 `CommandRunner`。

## 开发与阅读

```bash
python -m pip install -e ".[dev,tui]"
python -m pytest
```

测试覆盖工具执行与校验、沙箱、会话恢复、模型循环、上下文计量与压缩、输出截断、模式、图片内容和插话等；模型循环测试通过 `FauxProvider` 构造确定性事件。TUI 测试直接导入 Textual 界面模块，因此运行完整测试集需要安装 `tui` 可选依赖。

| 文档 | 内容 |
| --- | --- |
| [Agent Runtime](BLOG_Day1_Agent_Runtime.md) | 核心运行时的开发记录 |
| [Windows UTF-8 命令输出问题](BLOG_Day2_UTF8_Bash_Bug.md) | 编码问题的排查与处理 |
| [上下文 Anchor + Delta](BLOG_Day3_Context_Anchor_Delta.md) | 上下文计量设计记录 |
| [上下文管理实施计划](docs/context-management-plan.md) | 设计与分阶段规划 |
| [上下文管理调研索引](docs/research/README.md) | 外部实现调研及综合建议 |

开发记录与调研文档保留了阶段性方案；当前运行行为以源码和测试为准。提交功能变更时，请同时更新对应测试和本 README 的参数或行为说明。

## 许可

仓库目前未附带独立的 LICENSE 文件，也未在打包配置中声明许可证。


111
