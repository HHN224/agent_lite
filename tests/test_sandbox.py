"""sandbox 接缝的单元测试：三档 runner + 自动探测。

不依赖真实 docker/wsl（monkeypatch 探测函数与 subprocess.run），
用确定性输入钉住：探测顺序、强制指定不可用报错、WSL 路径映射、
HostRunner 的 cwd、DockerRunner argv 含隔离参数、命令失败回填格式。
"""

import pytest


def test_wsl_utf16_diagnostic_and_utf8_command_error_are_both_readable():
    from coding_agent.sandbox import _decode_bytes
    diagnostic = "wsl: 检测到 localhost 代理配置\r\n"
    command_error = "sh: command not found\n"
    raw = diagnostic.encode("utf-16-le") + command_error.encode("utf-8")
    assert _decode_bytes(raw) == diagnostic + command_error

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

def test_wsl_runner_uses_bubblewrap_for_workspace(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        class P:
            returncode = 0
            stdout = b"ok"
            stderr = b""
        return P()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    runner = WslRunner(tmp_path)
    result = runner.run("pwd")

    args = captured["args"]
    assert args[:3] == ["wsl", "-e", "bwrap"]
    assert "--unshare-all" in args
    assert "--clearenv" in args
    bind = args.index("--bind")
    assert args[bind + 1:bind + 3] == [windows_to_wsl(tmp_path), "/workspace"]
    assert ("--tmpfs", "/mnt") in zip(args, args[1:])
    assert args[args.index("--chdir") + 1] == "/workspace"
    assert args[-3:] == ["sh", "-lc", "pwd"]
    assert "shell" not in captured["kwargs"]
    assert result.stdout == "ok"


def test_wsl_available_requires_bubblewrap(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        class P:
            returncode = 0
        return P()

    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    from coding_agent.sandbox import _wsl_available

    assert _wsl_available() is True
    assert captured["args"] == [
        "wsl", "-e", "sh", "-lc", "command -v bwrap >/dev/null 2>&1",
    ]


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


# ---------- 秘密掩蔽（内核级，不依赖用户点 yes）----------

def _workspace_with_secrets(tmp_path):
    (tmp_path / ".env").write_text("CMD_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "cert.pem").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "abc.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    return tmp_path


def _capture_run(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args

        class P:
            returncode = 0
            stdout = b"ok"
            stderr = b""

        return P()
    monkeypatch.setattr("coding_agent.sandbox.subprocess.run", fake_run)
    return captured


def _has_ro_null(args, dest: str) -> bool:
    """存在 `--ro-bind /dev/null <dest>`（把文件掩蔽成空内容）。"""
    return any(args[i] == "--ro-bind" and args[i + 1] == "/dev/null" and args[i + 2] == dest
               for i in range(len(args) - 2))


def _has_tmpfs(args, dest: str) -> bool:
    """存在 `--tmpfs <dest>`（把目录掩蔽成空目录）。"""
    return any(args[i] == "--tmpfs" and args[i + 1] == dest for i in range(len(args) - 1))


def test_wsl_runner_masks_secrets_and_session_archive(tmp_path, monkeypatch):
    workspace = _workspace_with_secrets(tmp_path)
    captured = _capture_run(monkeypatch)
    runner = WslRunner(workspace, mask=[workspace / "sessions"])
    runner.run("cat .env")

    args = captured["args"]
    # 文件：/dev/null 覆盖（读到空内容）
    assert _has_ro_null(args, "/workspace/.env")
    assert _has_ro_null(args, "/workspace/cert.pem")
    # 目录：空 tmpfs（看到空目录，写入不落盘）
    assert _has_tmpfs(args, "/workspace/sessions")
    assert _has_tmpfs(args, "/workspace/.ssh")
    # 普通源码不受影响
    assert not _has_ro_null(args, "/workspace/app.py")
    # 依赖目录进 PYTHONPATH（受控取件的落地点）
    assert "/workspace/.agent-lite/deps/python" in args


def test_wsl_masking_can_be_disabled_by_absence(tmp_path, monkeypatch):
    """没有敏感文件时不应凭空多出掩蔽参数。"""
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    captured = _capture_run(monkeypatch)
    WslRunner(tmp_path).run("ls")
    args = captured["args"]
    assert "/dev/null" not in args
    assert args.count("--tmpfs") == 5  # 只有 /mnt /home /root /run /tmp 五个基础 tmpfs


def test_mask_ignores_paths_outside_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    captured = _capture_run(monkeypatch)
    WslRunner(workspace, mask=[outside]).run("ls")
    assert "/dev/null" not in captured["args"]


def test_docker_runner_masks_secrets(tmp_path, monkeypatch):
    workspace = _workspace_with_secrets(tmp_path)
    captured = _capture_run(monkeypatch)
    DockerRunner(workspace, mask=[workspace / "sessions"]).run("cat .env")

    args = captured["args"]
    assert "type=bind,source=/dev/null,target=/workspace/.env,readonly" in args
    assert _has_tmpfs(args, "/workspace/sessions")
    assert "PYTHONPATH=/workspace/.agent-lite/deps/python" in args


def test_host_runner_is_honest_about_not_masking(tmp_path):
    runner = HostRunner(tmp_path)
    assert runner.masks_secrets is False
    assert "掩蔽在本档**不生效**" in runner.describe()


def test_detect_backend_passes_mask_through(tmp_path, monkeypatch):
    workspace = _workspace_with_secrets(tmp_path)
    monkeypatch.setattr("coding_agent.sandbox._wsl_available", lambda: True)
    monkeypatch.setattr("coding_agent.sandbox._docker_available", lambda: False)
    runner = detect_backend("auto", workspace=workspace, mask=[workspace / "sessions"])
    assert isinstance(runner, WslRunner)
    assert ("sessions", "dir") in runner.masked_entries()
    assert (".env", "file") in runner.masked_entries()


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
