"""本地页面运行器：把 agent 写的 HTML 真的在浏览器里跑一遍。

为什么需要它 —— 来自真实 session 的教训（sessions/scene_browser_feedback*.txt）：
前端/可视化任务里 agent 只能靠静态推理，于是**人被迫当测试台**：手动开 Edge、抄控制台
堆栈、再粘回给 agent。3 轮往返只修掉 3 个运行时错误（着色器编译失败、未定义变量、
方法名写错），而这些错误 headless 跑一次就能全部拿到。同一批 session 里还花了 12 条
bash 命令去 `which chromium / import playwright` 找浏览器。

与既有的受控取件同一套思路（沙箱不联网，联网/运行时能力在沙箱外由我们的代码执行）：
  · 浏览器复用用户已装的 Edge / Chrome（channel: msedge / chrome），不下载 Chromium；
  · 页面发起的请求在浏览器层拦截：只放行本地地址与白名单域名，其余 abort + 记录；
  · 静态服务只绑定 127.0.0.1，并拒绝服务 .env / sessions/ / 密钥文件（与沙箱掩蔽同一份名单），
    否则页面里一句 fetch('/.env') 就能同源读到密钥；
  · 报告以文本为主（报错/警告/被拦请求/页面信息/交互是否改变画面），截图落盘给路径；
    只有小图才会被附加给模型 —— 实测模型吃图（user 消息里放图能答对颜色），
    但 tool 结果里 205KB 的图片曾触发过 HTTP 400，所以体积要克制。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import fetch as fetch_mod
from .sandbox import SENSITIVE_DIR_NAMES, SENSITIVE_FILE_PATTERNS

RUNNER_PATH = Path(__file__).with_name("browser_runner.cjs")

# 复用已安装的浏览器：优先 Edge（Windows 自带），其次 Chrome，最后才是 Playwright 自带 chromium
BROWSER_CHANNELS = ("msedge", "chrome", None)

DEFAULT_VIEWPORT = {"width": 1200, "height": 850}
DEFAULT_WAIT_MS = 2500
GOTO_TIMEOUT_MS = 30000
# 附加给模型的图片上限：超过就只给路径（避免重演 205KB tool 图片触发的 HTTP 400）
MAX_ATTACH_BYTES = 150 * 1024
SMALL_JPEG_QUALITY = 45


class BrowserError(Exception):
    """运行器不可用或页面运行失败：message 会原样回给模型。"""


# --------------------------------------------------------------------------- #
# 依赖探测（不猜：找不到就明确说缺什么、怎么补）
# --------------------------------------------------------------------------- #
def find_node() -> str | None:
    return os.environ.get("AGENT_LITE_NODE") or shutil.which("node")


def visible_playwright_candidates() -> list[str]:
    """按可靠性排序的 playwright 模块候选路径。"""
    candidates: list[str] = []
    override = os.environ.get("AGENT_LITE_PLAYWRIGHT")
    if override:
        candidates.append(override)
    # 常见的运行时缓存（Codex / Claude Code 之类会把 node_modules 留在缓存目录）
    for root in (Path.home() / ".cache", Path(os.environ.get("LOCALAPPDATA", "."))):
        for pattern in ("codex-runtimes/*/dependencies/node/node_modules/playwright",
                        "*/node_modules/playwright"):
            try:
                candidates.extend(str(p) for p in root.glob(pattern) if p.is_dir())
            except OSError:
                continue
    candidates.append("playwright")  # 交给 node 自己解析（全局或 cwd 安装）
    seen, unique = set(), []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _probe_playwright(node: str, candidates: list[str]) -> str | None:
    """用 node 实地 require 一遍，返回第一个能加载的模块路径。"""
    script = (
        "const list = JSON.parse(process.argv[1]);"
        "for (const item of list) {"
        "  try { require(item); console.log(item); process.exit(0); } catch (e) {}"
        "}"
        "process.exit(1);"
    )
    try:
        completed = subprocess.run(
            [node, "-e", script, json.dumps(candidates)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    loaded = (completed.stdout or "").strip().splitlines()
    return loaded[-1] if completed.returncode == 0 and loaded else None


def browser_status() -> tuple[bool, str, dict]:
    """检查运行器是否可用：返回 (ok, 说明, 细节)。"""
    node = find_node()
    if not node:
        return False, "没找到 node（页面运行器需要 Node + Playwright）", {}
    if not RUNNER_PATH.is_file():
        return False, f"运行器脚本缺失: {RUNNER_PATH}", {}
    playwright = _probe_playwright(node, visible_playwright_candidates())
    if not playwright:
        return False, (
            "没找到可加载的 playwright 模块。\n"
            "  安装方式任选其一：\n"
            "    1) npm i -g playwright && npx playwright install msedge\n"
            "    2) 设置 AGENT_LITE_PLAYWRIGHT 指向已有 node_modules/playwright 目录"
        ), {}
    return True, "ok", {"node": node, "playwright": playwright}


# --------------------------------------------------------------------------- #
# 静态服务：只绑定本机，且拒服务敏感文件
# --------------------------------------------------------------------------- #
def is_sensitive_relative(relative: str) -> bool:
    """请求路径是否命中密钥/存档掩蔽名单（与沙箱掩蔽共用同一份模式）。

    额外加上 sessions/、.git/、.agent-lite/：沙箱里前者是靠显式路径掩蔽的，
    而静态服务必须**同样**拒绝，否则页面里一句 fetch('/sessions/<id>.json')
    就能同源读到全部对话存档（.git 里也可能留着历史密钥）。
    """
    parts = [p for p in relative.replace("\\", "/").split("/") if p not in ("", ".")]
    denied_dirs = (".git", ".agent-lite", "sessions") + tuple(SENSITIVE_DIR_NAMES)
    for part in parts:
        if any(fnmatch.fnmatch(part, pattern) for pattern in SENSITIVE_FILE_PATTERNS):
            return True
        if part in denied_dirs:
            return True
    return False


class _WorkspaceHandler(SimpleHTTPRequestHandler):
    """workspace 的只读静态服务：拒绝敏感路径，不做目录列表。"""

    def __init__(self, *args, workspace: Path, **kwargs):
        super().__init__(*args, directory=str(workspace), **kwargs)

    def _denied(self) -> bool:
        relative = self.path.split("?", 1)[0].lstrip("/")
        return is_sensitive_relative(relative)

    def list_directory(self, path):  # noqa: D102 - 不提供目录列表
        self.send_error(403, "Directory listing disabled")
        return None

    def send_head(self):
        if self._denied():
            self.send_error(403, "Forbidden")
            return None
        return super().send_head()

    def end_headers(self):
        # 每次都要拿到最新产物：agent 改完文件马上重跑
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):  # 静音：日志由报告承担
        pass


class WorkspaceServer:
    """把工作区用 http://127.0.0.1:<随机端口>/ 服务出来（页面需要同源 fetch / ES module）。"""

    def __init__(self, workspace: Path, root_subpath: str = ""):
        self.workspace = Path(workspace).resolve()
        self.root_subpath = root_subpath.strip("/")

    def __enter__(self) -> str:
        handler = partial(_WorkspaceHandler, workspace=self.workspace)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        base = f"http://127.0.0.1:{self.port}/"
        return base + (self.root_subpath + "/" if self.root_subpath else "")

    def __exit__(self, *exc):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #
def _normalize_actions(actions) -> list[dict]:
    """校验并归一化交互动作（模型给的东西一律当不可信输入）。"""
    if actions in (None, ""):
        return []
    if not isinstance(actions, list):
        raise BrowserError("actions 必须是数组")
    if len(actions) > 12:
        raise BrowserError("actions 最多 12 个")
    normalized = []
    for item in actions:
        if not isinstance(item, dict):
            raise BrowserError("actions 的每一项必须是对象")
        kind = item.get("type")
        if kind == "drag":
            try:
                start = [int(item["from"][0]), int(item["from"][1])]
                end = [int(item["to"][0]), int(item["to"][1])]
            except (KeyError, TypeError, ValueError, IndexError):
                raise BrowserError("drag 需要 from:[x,y] 与 to:[x,y]")
            normalized.append({"type": "drag", "from": start, "to": end})
        elif kind == "wheel":
            normalized.append({"type": "wheel", "dx": int(item.get("dx", 0)), "dy": int(item.get("dy", 0))})
        elif kind == "click":
            try:
                normalized.append({"type": "click", "at": [int(item["at"][0]), int(item["at"][1])]})
            except (KeyError, TypeError, ValueError, IndexError):
                raise BrowserError("click 需要 at:[x,y]")
        elif kind == "wait":
            normalized.append({"type": "wait", "ms": max(0, min(int(item.get("ms", 300)), 5000))})
        else:
            raise BrowserError(f"不支持的 action: {kind!r}（可用 drag / wheel / click / wait）")
    return normalized


def render_page(
    workspace: Path,
    page: str,
    wait_ms: int = DEFAULT_WAIT_MS,
    actions=None,
    allowed_hosts: list[str] | None = None,
    screenshot: bool = True,
    viewport: dict | None = None,
) -> dict:
    """在真实浏览器里打开工作区内的一个页面，返回结构化报告。

    报告字段：ok / errors / warnings / blocked / contacted / page / screenshots /
    changed / channel / load_ms，以及 fatal（不可恢复的失败原因）。
    """
    workspace = Path(workspace).resolve()
    page_path = (workspace / page).resolve()
    if workspace != page_path and workspace not in page_path.parents:
        raise BrowserError(f"页面必须位于工作目录内: {page}")
    if not page_path.is_file():
        raise BrowserError(f"找不到页面: {page}")

    ok, why, detail = browser_status()
    if not ok:
        raise BrowserError(why)

    relative = page_path.relative_to(workspace).as_posix()
    actions = _normalize_actions(actions)
    hosts = fetch_mod.allowed_hosts(list(allowed_hosts or ()))
    viewport = viewport or DEFAULT_VIEWPORT
    wait_ms = max(0, min(int(wait_ms), 15000))

    out_dir = workspace / ".agent-lite" / "browser"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shots = {
        "screenshot_full": str(out_dir / f"{stamp}-full.png"),
        "screenshot_small": str(out_dir / f"{stamp}-small.jpg"),
        "screenshot_after": str(out_dir / f"{stamp}-after.png") if actions else None,
    }

    parent = page_path.parent.relative_to(workspace).as_posix() if page_path.parent != workspace else ""
    with WorkspaceServer(workspace, parent) as base_url:
        config = {
            "url": base_url + page_path.name,
            "playwright": detail["playwright"],
            "channels": list(BROWSER_CHANNELS),
            "allowed_hosts": list(hosts),
            "viewport": viewport,
            "wait_ms": wait_ms,
            "goto_timeout_ms": GOTO_TIMEOUT_MS,
            "actions": actions,
            "jpeg_quality": SMALL_JPEG_QUALITY,
            **shots,
        }
        with tempfile.TemporaryDirectory(prefix="agent-lite-browser-") as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            completed = _run_node(detail["node"], config_path)

    report = _parse_report(completed)
    report["page_file"] = relative
    report["screenshots"] = {k: v for k, v in report.get("screenshots", {}).items() if v}
    _audit(workspace, relative, report)
    return report


def _run_node(node: str, config_path: Path):
    return subprocess.run(
        [node, str(RUNNER_PATH), str(config_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=str(RUNNER_PATH.parent), stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _parse_report(completed) -> dict:
    raw = (completed.stdout or "").strip()
    if not raw:
        detail = (completed.stderr or "").strip()[-600:]
        raise BrowserError(f"页面运行器没有输出（node exit={completed.returncode}）\n{detail}")
    try:
        return json.loads(raw.splitlines()[-1])
    except ValueError as exc:
        raise BrowserError(f"页面运行器输出不是 JSON: {exc}\n{raw[:400]}") from exc


def _audit(workspace: Path, page: str, report: dict) -> None:
    fetch_mod.append_audit(workspace, {
        "kind": "render-page",
        "ok": bool(report.get("ok")),
        "page": page,
        "channel": report.get("channel"),
        "errors": len(report.get("errors") or []),
        "blocked": len(report.get("blocked") or []),
        "contacted": sorted({
            re.sub(r"^(https?://[^/]+).*", r"\1", url) for url in (report.get("contacted") or [])
        }),
    })


# --------------------------------------------------------------------------- #
# 报告 -> 回给模型的文本 / 可选的小图
# --------------------------------------------------------------------------- #
def format_report(report: dict) -> str:
    """把报告渲染成模型能读的文本（文本优先：不吃图的模型也能闭环）。"""
    if report.get("fatal"):
        return f"Error: 页面运行失败：{report['fatal']}"

    lines = [
        f"Page: {report.get('page_file')} (channel={report.get('channel')}, "
        f"load={report.get('load_ms')}ms)",
    ]
    page = report.get("page") or {}
    if page.get("title"):
        lines.append(f"Title: {page['title']}")
    canvases = page.get("canvases") or []
    if canvases:
        lines.append("Canvas: " + ", ".join(f"{c.get('width')}x{c.get('height')}" for c in canvases))
    elif page.get("elements") is not None:
        lines.append(f"Canvas: (none) · DOM elements: {page.get('elements')}")

    errors = report.get("errors") or []
    lines.append("")
    if errors:
        lines.append(f"❌ BROWSER ERRORS ({len(errors)}) —— 这些是页面真的跑不起来的原因：")
        for item in errors[:12]:
            lines.append("  - " + str(item)[:400])
        if len(errors) > 12:
            lines.append(f"  ... 另有 {len(errors) - 12} 条")
    else:
        lines.append("✅ 没有 pageerror / console.error（初始化和首帧未抛错）")

    warnings = report.get("warnings") or []
    if warnings:
        lines.append(f"⚠️ console warnings ({len(warnings)}): "
                     + " | ".join(str(w)[:160] for w in warnings[:4]))

    blocked = report.get("blocked") or []
    if blocked:
        lines.append(f"🚫 被拦截的请求 ({len(blocked)}):")
        for item in blocked[:6]:
            lines.append(f"  - {item.get('url')} ({item.get('reason')})")

    if report.get("changed") is not None:
        lines.append("")
        lines.append(
            "交互验证: 拖拽/滚轮之后画面" + ("**发生了变化**（渲染循环与交互都活着）"
                                            if report["changed"] else
                                            "**没有任何变化**（可能没绑定交互，或渲染循环没跑）")
        )

    text = (page.get("text") or "").strip()
    if text:
        lines.append("")
        lines.append("首屏可见文本(前 400 字): " + text[:400].replace("\n", " / "))

    shots = report.get("screenshots") or {}
    if shots:
        lines.append("")
        lines.append("截图: " + ", ".join(f"{k}={v}" for k, v in shots.items()))
    return "\n".join(lines)


def attach_small_screenshot(workspace: Path, report: dict) -> dict | None:
    """体积足够小才把截图附加给模型；否则只留路径（避免重演 205KB 图片的 HTTP 400）。"""
    small = (report.get("screenshots") or {}).get("small")
    if not small:
        return None
    path = Path(small)
    if not path.is_file() or path.stat().st_size > MAX_ATTACH_BYTES:
        return None
    import base64

    from agent_core.content import image_block

    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return image_block(data, "image/jpeg")
