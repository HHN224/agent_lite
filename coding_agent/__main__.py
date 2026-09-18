import argparse
import os
import re
import sys
import traceback
from pathlib import Path

# 支持直接运行本文件（如 VS Code 运行按钮）：把项目根目录加入 sys.path，
# 否则找不到 agent_core、coding_agent 等同级包。推荐入口仍是 python -m coding_agent。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from ai import CommandCodeProvider, OpenAIProvider
from agent_core import (
    Agent,
    AgentEvent,
    AgentLoop,
    CompactionEngine,
    ContextManager,
    SessionRepository,
    ToolOutputTruncator,
    ToolResultStore,
    make_summarizer,
    workspace_state_dir,
)
from coding_agent.modes import get_mode, mode_names
from coding_agent.sandbox import detect_backend
# textual TUI 为可选依赖；缺失时回退到 CLI（不影响纯命令行使用）
try:
    from coding_agent.tui import run_tui
    _HAS_TUI = True
except ImportError:
    _HAS_TUI = False
    def run_tui(*a, **k):
        raise RuntimeError("textual 未安装，请 pip install textual 后使用 --ui tui")
from coding_agent.tools import build_tools


SYSTEM_PROMPT = get_mode("default")


def safe_print(s: str, **kwargs):
    """按终端编码打印，无法编码的字符用替换符兜底，避免 GBK 终端上 print 抛 UnicodeEncodeError。"""
    try:
        print(s, **kwargs)
    except UnicodeEncodeError:
        print(s.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"), **kwargs)


def _content_text(content) -> str:
    """把消息 content（str 或结构块列表）提取为纯文本，供终端显示。

    图片块显示为 [image:<mime>]，thinking 块不在此显示（由 thinking_start/update 单列）。
    避免在 GBK 终端上打印超长 base64 出问题。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "image_url":
            url = b.get("image_url", {})
            parts.append(f"[image:{url.get('url', '')[:40]}]")
        elif t == "tool_result":
            parts.append(b.get("content") or "")
    return "".join(parts)


def cli_listener(event: AgentEvent):
    """把 Agent 的事件流渲染成终端输出（复刻原来的 >>> 提示格式）。"""
    if event.type == "turn_start":
        print("\n>>> 调用 API ...")
    elif event.type == "message_update":
        safe_print(_content_text(event.data["content"]), end="", flush=True)
    elif event.type == "message_end":
        print()
    elif event.type == "thinking_start":
        safe_print("\n>>> 思考中...", end="", flush=True)
    elif event.type == "thinking_update":
        safe_print(_content_text(event.data["content"]), end="", flush=True)
    elif event.type == "thinking_end":
        print()
    elif event.type == "tool_execution_start":
        safe_print(f">>> 正在使用工具: {event.data['name']} | 参数: {event.data['arguments']}")
    elif event.type == "tool_execution_end":
        suffix = "（已截断）" if event.data.get("pruned") else ""
        full_path = event.data.get("full_output_path")
        if full_path:
            suffix += f" —— 全文: {full_path}"
        safe_print(f">>> 工具返回: {_content_text(event.data['content'])[:200]}{suffix}")
    elif event.type == "error":
        safe_print(f"\n>>> 模型服务出错: {event.data['message']}")
    elif event.type == "context_check":
        d = event.data
        safe_print(
            f">>> 上下文: {d['total_tokens']} / {d['context_window']} tokens "
            f"({d['ratio']:.0%}) | 阈值 {d['threshold_ratio']:.0%} "
            f"| {'将压缩' if d['needs_compaction'] else '正常'}"
        )
    elif event.type == "compaction_start":
        safe_print(f">>> 正在压缩上下文（折叠前 {event.data['compacted_count']} 条）...")
    elif event.type == "compaction_end":
        d = event.data
        if d["success"]:
            folded = d.get("folded_messages", d["compacted_count"])
            safe_print(f">>> 压缩完成：已折叠 {folded} 条消息，保留从 {d['first_kept_entry_id']} 起")
        else:
            failures = d.get("consecutive_failures", 1)
            safe_print(f">>> 压缩未生效（连续第 {failures} 次），已保留原文继续：{d.get('reason') or '未知原因'}")
    elif event.type == "compaction_skip":
        safe_print(f">>> 跳过压缩：{event.data['reason']}")
    elif event.type == "compaction_paused":
        d = event.data
        if d.get("disabled"):
            safe_print(f">>> 压缩已停手（任务不受影响）：{d.get('reason')}")
            safe_print(">>> 需要时可用 /compact 手动再试一次")
        else:
            safe_print(f">>> 压缩暂时跳过：{d.get('reason')}")
# 项目根目录下的 .env 文件（无论从哪里运行都能加载到）
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 会话存档目录：sessions/<session_id>.json
SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"

# session_id 白名单（uuid hex，纯字母数字，防止路径越界）
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Agent Lite 交互式 REPL")
    parser.add_argument("--provider", choices=["deepseek", "commandcode"], default="commandcode", help="模型服务（默认 commandcode，使用 GOAT 套餐 CMD_API_KEY）")
    parser.add_argument(
        "--session",
        default=os.environ.get("AGENT_SESSION", ""),
        help="恢复到指定会话 id（sessions/<id>.json）；不填则恢复最近一次（配合 --new 新建）",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="不恢复历史，新建一个会话（仍会存档到磁盘）",
    )
    parser.add_argument(
        "--name",
        default="",
        help="新建会话的展示名（可选）",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="工作目录：文件工具与 bash 沙箱的活动范围（默认当前目录）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="模型名（随 provider 默认选择 DeepSeek V4.1 Flash）",
    )
    parser.add_argument(
        "--mode",
        default="default",
        help="任务模式：default / plan / code / review（默认 default）",
    )
    parser.add_argument(
        "--ui",
        choices=["tui", "cli"],
        default="tui",
        help="界面：tui（textual，默认）/ cli（行式 REPL）",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="覆盖所选 provider 的 API 地址",
    )
    parser.add_argument(
        "--permission-policy",
        choices=["ask", "deny", "auto"],
        default="ask",
        help="危险工具的权限策略：ask 每次确认 / deny 直接拒绝 / auto 自动放行（默认 ask）",
    )
    parser.add_argument(
        "--bypass",
        action="store_const",
        const="auto",
        dest="permission_policy",
        help="自动通过所有工具权限请求（等同 --permission-policy auto）",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=128000,
        help="模型上下文窗口（token），用于触发上下文压缩的计量（默认 128000，0 表示不启用）",
    )
    parser.add_argument(
        "--compact-threshold",
        type=float,
        default=0.8,
        help="触发压缩的窗口占用阈值（0~1，默认 0.8）",
    )
    parser.add_argument(
        "--compact-retain",
        type=float,
        default=0.16,
        help="压缩时保留尾部原文的窗口占比（默认 0.16，DSH）",
    )
    parser.add_argument(
        "--compact-max-tokens",
        type=int,
        default=2000,
        help="压缩摘要的最大 token 数（默认 2000）",
    )
    parser.add_argument(
        "--compact-fallback",
        choices=["digest", "none"],
        default="digest",
        help=(
            "模型摘要连续失败后怎么办：digest 用机械摘要兜底继续压缩（默认，保证上下文能压下去、"
            "任务不被拖死）；none 则彻底停手，只保留原文（严格档，上下文会一直增长）"
        ),
    )
    parser.add_argument(
        "--bash-image",
        default="python:3.12-slim",
        help="bash 工具使用的 Docker 运行镜像（默认 python:3.12-slim，仅 --sandbox=docker 时生效）",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=None,
        metavar="DOMAIN",
        help=(
            "扩展受控取件的域名白名单（可重复），例如 --allow-host example.com。"
            "默认只放行 pypi / npm / github 等取依赖必需的注册表；"
            "install / fetch / render_page 三个工具受此白名单约束，沙箱本身始终没有网络出网能力"
        ),
    )
    parser.add_argument(
        "--no-browser-image",
        action="store_true",
        help="render_page 不把截图附加给模型（只给路径），用于模型不支持图片输入时",
    )
    parser.add_argument(
        "--sandbox",
        choices=["auto", "host", "wsl", "docker"],
        default="auto",
        help="bash 命令的沙箱后端：auto 自动探测（docker→wsl→host）/ host 宿主直跑 / wsl WSL2 / docker Docker（默认 auto）",
    )
    args = parser.parse_args(argv)
    if args.model is None:
        args.model = CommandCodeProvider.DEFAULT_MODEL if args.provider == "commandcode" else "deepseek-flash"
    if args.base_url is None:
        args.base_url = CommandCodeProvider.DEFAULT_BASE_URL if args.provider == "commandcode" else "https://api.deepseek.com"

    if args.session and not SESSION_ID_RE.match(args.session):
        parser.error("会话 id 只能是字母/数字，长度 1-64")

    args.workspace = args.workspace.resolve()
    if not args.workspace.is_dir():
        parser.error(f"工作目录不存在: {args.workspace}")

    return args


def make_confirm():
    """生成权限确认函数：展示工具调用的人类可读描述，等待用户 y/N。
    非交互环境（EOF / Ctrl+C）一律视为拒绝，保证不产生副作用。"""

    def confirm(description: str) -> bool:
        try:
            answer = input(f">>> {description}\n>>> 是否允许执行？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")

    return confirm


def pick_initial_session(repo: SessionRepository, args):
    """决定初始会话，返回 (session, is_new)。

    优先级：--session 指定 → 否则若非 --new 则恢复最近一次 → 否则新建。
    """
    if args.session:
        s = repo.load(args.session)
        if s is None:
            sys.exit(f"错误：找不到会话 '{args.session}'（不存在于 {SESSIONS_DIR}）")
        return s, False

    if not args.new:
        metas = repo.list()
        if metas:
            return repo.load(metas[0].session_id), False

    return repo.create(name=args.name, system_prompt=get_mode(args.mode)), True


def build_agent(args, api_key):
    # 自底向上组装：ai 层 Provider → agent_core 循环 → Agent 对话状态 + 会话存档
    provider_class = CommandCodeProvider if args.provider == "commandcode" else OpenAIProvider
    provider = provider_class(
        api_key=api_key,
        base_url=args.base_url,
    )

    # TUI postpones sandbox probing until the first shell tool call.
    def make_runner():
        return detect_backend(
            sandbox=args.sandbox,
            workspace=args.workspace,
            bash_image=args.bash_image,
            # 会话存档（含全部对话与工具输出）也掩蔽掉：它通常就在工作区内
            mask=[SESSIONS_DIR],
        )

    if args.ui == "tui" and _HAS_TUI:
        from coding_agent.tui import LazyCommandRunner
        runner = LazyCommandRunner(make_runner, args.workspace, args.sandbox)
    else:
        try:
            runner = make_runner()
        except Exception as e:
            raise RuntimeError(f"错误：{e}") from e

    tools = build_tools(
        args.workspace,
        bash_image=args.bash_image,
        runner=runner,
        allowed_hosts=args.allow_host,
    )
    if args.no_browser_image:
        for tool in tools:
            if tool.name == "render_page":
                tool.attach_image = False

    loop = AgentLoop(
        provider=provider,
        model=args.model,
        tools=tools,
        permission_policy=args.permission_policy,
        confirm=make_confirm(),
    )

    repo = SessionRepository(SESSIONS_DIR)
    session, is_new = pick_initial_session(repo, args)
    if not is_new:
        # Re-estimate the next payload after reopening. Older releases counted
        # display-only reasoning as input tokens and persisted inflated caches.
        session.usage = 0
        session.new_usage = 0
    if is_new:
        repo.save(session)  # 新建的立即落盘

    # Phase 2：工具输出管理 —— 全文写进**工作目录内**的 .agent-lite/，模型见 preview + 路径。
    # 写在工作目录之外时 read 的 safe_path 会拒绝，模型就永远读不回自己的工具输出。
    loop.session_id = session.session_id
    loop.truncator = ToolOutputTruncator(
        store=ToolResultStore(
            workspace_state_dir(args.workspace), workspace=args.workspace
        ),
    )

    # 上下文管理（阶段 A）：计量 + 触发。阈值做成配置，窗口默认 128000。
    context_manager = ContextManager(threshold_ratio=args.compact_threshold)

    # 阶段 B：真压缩引擎。摘要走**独立 provider 实例**：与主循环共用实例时，
    # 摘要调用的 usage 会写进 provider.last_usage，而那是上下文计量校准 session.usage
    # 的真实锚点（被覆盖 = 计量失真）。摘要模型与主体模型相同，但 temperature=0、
    # max_tokens 真正生效，且整条请求不带工具（见 make_summarizer）。
    summary_provider = provider_class(api_key=api_key, base_url=args.base_url)
    compaction_engine = CompactionEngine(
        summarizer=make_summarizer(
            provider=summary_provider,
            model=args.model,
            max_tokens=args.compact_max_tokens,
        ),
        retain_ratio=args.compact_retain,
        fallback=args.compact_fallback,
    )

    agent = Agent(
        loop=loop,
        session=session,
        repo=repo,
        context_manager=context_manager,
        context_window=args.context_window,
        compaction_engine=compaction_engine,
    )

    return agent


def main():
    args = parse_args()

    # 优先加载项目根目录 .env（源码直跑场景），再兜底加载当前目录 .env（全局安装后任意目录启动）
    load_dotenv(ENV_FILE)
    load_dotenv()

    key_name = "CMD_API_KEY" if args.provider == "commandcode" else "DEEPSEEK_API_KEY"
    api_key = os.getenv(key_name)
    if not api_key:
        sys.exit(
            f"错误：未检测到 {key_name}。\n"
            f"请在项目根目录 .env 中设置 {key_name}（参考 .env.example）。"
        )

    # Paint the TUI before SDK setup, session I/O and Docker / WSL probing.
    if args.ui == "tui" and _HAS_TUI:
        run_tui(agent_factory=lambda: build_agent(args, api_key), workspace=args.workspace)
        return

    agent = build_agent(args, api_key)
    session, repo, loop = agent.session, agent.repo, agent.loop
    runner = next(tool.runner for tool in loop.tools if tool.name == "bash")

    print(f">>> 会话: {session.session_id}（{session.name or '<未命名>'}，{session.message_count} 条消息）")
    print(f">>> 存档: {repo._path(session.session_id)}")
    print(f">>> 工作目录: {args.workspace}")
    print(f">>> 权限策略: {args.permission_policy}")
    print(f">>> bash 沙箱: {runner.mode} —— {runner.describe()}")
    print(f">>> 上下文窗口: {args.context_window}（阈值 {args.compact_threshold:.0%}，保留 {args.compact_retain:.0%}）")
    print(f">>> 模式: {args.mode}（/mode 切换，可选: {', '.join(mode_names())}）")
    print(f">>> 输入 /sessions 查看 / 重命名 / 删除会话")
    print(f">>> 输入 /deps 查看受控取件的依赖与审计日志")

    # 当前任务模式（/mode 会更新）；新建会话用 get_mode(current_mode) 作为 system prompt
    current_mode = args.mode

    if args.ui == "tui" and not _HAS_TUI:
        print(">>> textual 未安装，回退到 CLI。请 pip install textual（或 pip install -e .[tui]）")
        # 继续走 CLI（fallthrough）

    while True:
        try:
            user_input = input("> ")
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            break

        if user_input.strip() == "/clear":
            agent.clear_history()
            print(">>> 已清空对话历史")
            continue

        if user_input.strip() == "/new":
            session = repo.create(system_prompt=get_mode(current_mode))
            repo.save(session)
            agent.session = session
            print(f">>> 已新建会话: {session.session_id}（mode: {current_mode}）")
            continue

        if user_input.strip().startswith("/mode"):
            parts = user_input.strip().split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print(f">>> 用法: /mode <{'/'.join(mode_names())}>（当前: {current_mode}）")
                continue
            new_mode = parts[1].strip()
            if new_mode not in mode_names():
                print(f">>> 未知 mode: {new_mode}，可选: {', '.join(mode_names())}")
                continue
            current_mode = new_mode
            agent.session.system_prompt = get_mode(current_mode)
            agent._save()
            print(f">>> 已切换到 mode: {current_mode}")
            continue

        if user_input.strip().startswith("/sessions"):
            _cmd_sessions(repo, agent)
            continue

        if user_input.strip().startswith("/deps"):
            _cmd_deps(args.workspace, user_input.strip())
            continue

        if user_input.strip().startswith("/name"):
            _cmd_name(repo, agent, user_input.strip())
            continue

        if user_input.strip().startswith("/delete"):
            _cmd_delete(repo, agent, user_input.strip())
            continue

        if user_input.strip() == "/compact":
            _manual_compact(agent)
            continue

        # 执行阶段：Ctrl+C 触发 abort（中止本轮），而非退出 REPL
        gen = agent.prompt(user_input)
        try:
            for event in gen:
                cli_listener(event)
        except KeyboardInterrupt:
            print("\n>>> 已中止")
            loop.abort()
            # 排空生成器剩余事件，让 loop 在下个检查点安全退出
            try:
                for event in gen:
                    cli_listener(event)
            except KeyboardInterrupt:
                pass
        except Exception:
            # 任何未预期错误都只跳过本轮，不退出会话
            print("\n发生未预期错误，已跳过本轮：")
            traceback.print_exc()


def _cmd_sessions(repo: SessionRepository, agent):
    metas = repo.list()
    if not metas:
        print(">>> 暂无会话")
        return
    print(">>> 会话列表（最新在前）：")
    for m in metas:
        mark = " *" if m.session_id == agent.session_id else ""
        print(f"   {m.session_id}  {m.name or '<未命名>':<16}  {m.message_count} 条消息  {m.updated_at:.0f}{mark}")


def _manual_compact(agent):
    """/compact：手动触发一次压缩。

    force=True 是关键：自动压缩失败后会进入退避甚至停手，但用户手动要求时必须真的
    再试一次模型摘要，否则「手动再试」就成了空话。
    """
    if agent.compaction_engine is None:
        print(">>> 未配置上下文压缩")
        return
    print(">>> 手动压缩 ...")
    for ev in agent.compaction_engine.compact_if_needed(
        agent.session, agent.context_window, force=True
    ):
        cli_listener(ev)
    agent._save()


def _cmd_deps(workspace, raw: str):
    """/deps 查看受控取件的产物与审计；/deps clear 清空（可逆）。"""
    from coding_agent import fetch as fetch_mod

    parts = raw.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip() in ("clear", "clean"):
        fetch_mod.clear_deps(workspace)
        print(">>> 已清空 .agent-lite/deps（wheel 缓存与解包产物）")
        return
    safe_print(fetch_mod.deps_summary(workspace))
    print(">>> 用法: /deps 查看 | /deps clear 清空依赖")


def _cmd_name(repo: SessionRepository, agent, raw: str):
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        print(">>> 用法: /name <显示名>")
        return
    new_name = parts[1].strip()
    try:
        for m in repo.list():
            if m.name == new_name and m.session_id != agent.session_id:
                print(f">>> 名字' {new_name} '已被其他会话占用")
                return
        repo.rename(agent.session_id, new_name)
        agent.session.name = new_name
        print(f">>> 会话重命名为: {new_name}")
    except Exception as e:
        print(f">>> 重命名失败: {e}")


def _cmd_delete(repo: SessionRepository, agent, raw: str):
    parts = raw.split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else ""
    if not target:
        print(">>> 用法: /delete <session_id>（当前会话 * ）")
        return
    if target == agent.session_id:
        print(">>> 不能删除当前正在使用的会话；请先 /new 另起一个")
        return
    exists = any(m.session_id == target for m in repo.list())
    if not exists:
        print(f">>> 会话不存在: {target}")
        return
    try:
        if make_confirm()(f"确认删除会话 {target}？"):
            repo.delete(target)
            print(f">>> 已删除会话 {target}")
        else:
            print(">>> 已取消")
    except Exception as e:
        print(f">>> 删除失败: {e}")


if __name__ == "__main__":
    main()
