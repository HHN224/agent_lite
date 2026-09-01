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

from ai import OpenAIProvider
from agent_core import Agent, AgentEvent, AgentLoop, SessionRepository
from coding_agent.sandbox import detect_backend
from coding_agent.tools import build_tools


SYSTEM_PROMPT = "You are a helpful AI agent with file and shell tools. Use them when needed, then answer concisely in the user's language."


def safe_print(s: str, **kwargs):
    """按终端编码打印，无法编码的字符用替换符兜底，避免 GBK 终端上 print 抛 UnicodeEncodeError。"""
    try:
        print(s, **kwargs)
    except UnicodeEncodeError:
        print(s.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"), **kwargs)


def cli_listener(event: AgentEvent):
    """把 Agent 的事件流渲染成终端输出（复刻原来的 >>> 提示格式）。"""
    if event.type == "turn_start":
        print("\n>>> 调用 API ...")
    elif event.type == "message_update":
        safe_print(event.data["content"], end="", flush=True)
    elif event.type == "message_end":
        print()
    elif event.type == "tool_execution_start":
        safe_print(f">>> 正在使用工具: {event.data['name']} | 参数: {event.data['arguments']}")
    elif event.type == "tool_execution_end":
        safe_print(f">>> 工具返回: {event.data['content'][:200]}")
    elif event.type == "error":
        safe_print(f"\n>>> 模型服务出错: {event.data['message']}")

# 项目根目录下的 .env 文件（无论从哪里运行都能加载到）
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 会话存档目录：sessions/<session_id>.json
SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"

# session_id 白名单（uuid hex，纯字母数字，防止路径越界）
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Agent Lite 交互式 REPL")
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
        default="deepseek-v4-flash",
        help="模型名（默认 deepseek-v4-flash）",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.deepseek.com",
        help="OpenAI 兼容 API 的 base_url（默认 DeepSeek）",
    )
    parser.add_argument(
        "--permission-policy",
        choices=["ask", "deny", "auto"],
        default="ask",
        help="危险工具的权限策略：ask 每次确认 / deny 直接拒绝 / auto 自动放行（默认 ask）",
    )
    parser.add_argument(
        "--bash-image",
        default="python:3.12-slim",
        help="bash 工具使用的 Docker 运行镜像（默认 python:3.12-slim，仅 --sandbox=docker 时生效）",
    )
    parser.add_argument(
        "--sandbox",
        choices=["auto", "host", "wsl", "docker"],
        default="auto",
        help="bash 命令的沙箱后端：auto 自动探测（docker→wsl→host）/ host 宿主直跑 / wsl WSL2 / docker Docker（默认 auto）",
    )
    args = parser.parse_args(argv)

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

    return repo.create(name=args.name, system_prompt=SYSTEM_PROMPT), True


def main():
    args = parse_args()

    # 优先加载项目根目录 .env（源码直跑场景），再兜底加载当前目录 .env（全局安装后任意目录启动）
    load_dotenv(ENV_FILE)
    load_dotenv()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit(
            "错误：未检测到 DEEPSEEK_API_KEY。\n"
            "请在项目根目录的 .env 文件中写入 DEEPSEEK_API_KEY=sk-xxxx（可参考 .env.example），\n"
            '或设置环境变量：$env:DEEPSEEK_API_KEY = "sk-xxxx"'
        )

    # 自底向上组装：ai 层 Provider → agent_core 循环 → Agent 对话状态 + 会话存档
    provider = OpenAIProvider(
        api_key=api_key,
        base_url=args.base_url,
    )

    # 探测沙箱后端（auto 会自动选 docker→wsl→host；强制指定不可用则 fail-closed 报错）
    try:
        runner = detect_backend(
            sandbox=args.sandbox,
            workspace=args.workspace,
            bash_image=args.bash_image,
        )
    except Exception as e:
        sys.exit(f"错误：{e}")

    tools = build_tools(args.workspace, bash_image=args.bash_image, runner=runner)

    loop = AgentLoop(
        provider=provider,
        model=args.model,
        tools=tools,
        permission_policy=args.permission_policy,
        confirm=make_confirm(),
    )

    repo = SessionRepository(SESSIONS_DIR)
    session, is_new = pick_initial_session(repo, args)
    if is_new:
        repo.save(session)  # 新建的立即落盘

    agent = Agent(loop=loop, session=session, repo=repo)

    print(f">>> 会话: {session.session_id}（{session.name or '<未命名>'}，{session.message_count} 条消息）")
    print(f">>> 存档: {repo._path(session.session_id)}")
    print(f">>> 工作目录: {args.workspace}")
    print(f">>> 权限策略: {args.permission_policy}")
    print(f">>> bash 沙箱: {runner.mode} —— {runner.describe()}")
    print(f">>> 输入 /sessions 查看 / 重命名 / 删除会话")

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
            session = repo.create(system_prompt=SYSTEM_PROMPT)
            repo.save(session)
            agent.session = session
            print(f">>> 已新建会话: {session.session_id}")
            continue

        if user_input.strip().startswith("/sessions"):
            _cmd_sessions(repo, agent)
            continue

        if user_input.strip().startswith("/name"):
            _cmd_name(repo, agent, user_input.strip())
            continue

        if user_input.strip().startswith("/delete"):
            _cmd_delete(repo, agent, user_input.strip())
            continue

        if user_input.strip() == "/compact":
            # 压缩的数据层已就绪；真正的 LLM 总结在后续阶段实现
            print(">>> 上下文压缩尚未实现（数据层已就绪，将在后续阶段接入）")
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
