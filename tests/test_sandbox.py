"""sandbox 接缝的单元测试：三档 runner + 自动探测。

不依赖真实 docker/wsl（monkeypatch 探测函数与 subprocess.run），
用确定性输入钉住：探测顺序、强制指定不可用报错、WSL 路径映射、
HostRunner 的 cwd、DockerRunner argv 含隔离参数、命令失败回填格式。
"""

import pytest

from coding_agent.sandbox import (
    DockerRunner,
    HostRunner,
    SandboxUnavailableError,
    WslRunner,
    detect_backend,
    windows_to_wsl,
)


# ---------- 路径映射 ----------

def test_windows_to_wsl_maps_drive():
    assert windows_to_wsl(r"D:\project space\agent lite") == "/mnt/d/project space/agent lite"


def test_windows_to_wsl_lowercases_drive():
    assert windows_to_wsl(r"C:\Users\x") == "/mnt/c/Users/x"


# ---------- HostRunner ----------

def test_host_runner_runs_in_workspace_cwd(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        captured["shell"] = kwargs.get("shell")
        from tests.test_tools import _FakeSubprocessResult  # reuse the fake
        return _FakeSubprocessResult(stdout=b"ok")

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    runner = HostRunner(tmp_path)
    result = runner.run("echo ok")
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["shell"] is True
    assert result.stdout == "ok"


# ---------- WslRunner ----------

def test_wsl_runner_composes_cd_with_quoted_path(tmp_path, monkeypatch):
    captured = {}
    import subprocess as sp

    def fake_run(args, **kwargs):
        captured["args"] = args
        class P:
            returncode = 0
            stdout = b"ok"
            stderr = b""
        return P()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    runner = WslRunner(tmp_path)
    result = runner.run("pwd")
    # wsl -e sh -lc 'cd "<mapped>" && pwd'
    assert captured["args"][0] == "wsl"
    assert captured["args"][1] == "-e"
    assert captured["args"][2] == "sh"
    assert captured["args"][3] == "-lc"
    full = captured["args"][4]
    assert full.startswith('cd "')
    assert windows_to_wsl(tmp_path) in full
    assert full.endswith('" && pwd')
    assert result.stdout == "ok"


# ---------- DockerRunner ----------

def test_docker_runner_argv_has_isolation_flags(tmp_path, monkeypatch):
    captured = {}
    import subprocess as sp

    def fake_run(args, **kwargs):
        captured["args"] = args
        class P:
            returncode = 0
            stdout = b"x"
            stderr = b""
        return P()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    runner = DockerRunner(tmp_path, image="python:3.12-slim")
    runner.run("echo x")

    args = captured["args"]
    assert args[0] == "docker" and args[1] == "run"
    assert "--network" in args and "none" in args
    assert "--read-only" in args
    assert "--tmpfs" in args and "/tmp" in args
    assert "--pids-limit" in args and "100" in args
    assert "--memory" in args and "512m" in args
    assert any("bind,source=" in a for a in args)
    assert args[args.index("--workdir") + 1] == "/workspace"
    assert "python:3.12-slim" in args
    # Docker run 结尾是 [image, sh, -lc, command]
    assert args[-4] == "python:3.12-slim"
    assert args[-3] == "sh"
    assert args[-2] == "-lc"
    assert args[-1] == "echo x"


def test_docker_runner_failure_backfilled_with_exit_code(tmp_path, monkeypatch):
    import subprocess as sp

    def fake_run(args, **kwargs):
        class P:
            returncode = 5
            stdout = b"some out"
            stderr = b""
        return P()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    runner = DockerRunner(tmp_path)
    result = runner.run("false")
    assert result.is_error is True
    assert result.exit_code == 5
    assert "exit code 5" in result.content


# ---------- detect_backend 探测顺序 ----------

def test_detect_backend_prefers_docker_when_available(monkeypatch):
    monkeypatch.setattr("coding_agent.sandbox._docker_available", lambda: True)
    monkeypatch.setattr("coding_agent.sandbox._wsl_available", lambda: True)
    runner = detect_backend("auto", workspace=".")
    assert isinstance(runner, DockerRunner)


def test_detect_backend_falls_back_to_wsl_when_no_docker(monkeypatch):
    monkeypatch.setattr("coding_agent.sandbox._docker_available", lambda: False)
    monkeypatch.setattr("coding_agent.sandbox._wsl_available", lambda: True)
    runner = detect_backend("auto", workspace=".")
    assert isinstance(runner, WslRunner)


def test_detect_backend_falls_back_to_host_when_none(monkeypatch):
    monkeypatch.setattr("coding_agent.sandbox._docker_available", lambda: False)
    monkeypatch.setattr("coding_agent.sandbox._wsl_available", lambda: False)
    runner = detect_backend("auto", workspace=".")
    assert isinstance(runner, HostRunner)


def test_detect_backend_forced_host_always_works(monkeypatch):
    runner = detect_backend("host", workspace=".")
    assert isinstance(runner, HostRunner)


def test_detect_backend_forced_wsl_fails_closed(monkeypatch):
    monkeypatch.setattr("coding_agent.sandbox._wsl_available", lambda: False)
    with pytest.raises(SandboxUnavailableError):
        detect_backend("wsl", workspace=".")


def test_detect_backend_forced_docker_fails_closed(monkeypatch):
    monkeypatch.setattr("coding_agent.sandbox._docker_available", lambda: False)
    with pytest.raises(SandboxUnavailableError):
        detect_backend("docker", workspace=".")


def test_detect_backend_forced_docker_uses_bash_image(monkeypatch):
    monkeypatch.setattr("coding_agent.sandbox._docker_available", lambda: True)
    runner = detect_backend("docker", workspace=".", bash_image="my:1.0")
    assert isinstance(runner, DockerRunner)
    assert runner.image == "my:1.0"
