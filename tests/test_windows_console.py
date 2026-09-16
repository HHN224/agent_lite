"""Exercise the Windows driver against an actual, hidden console, offline."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="native Windows console")
def test_native_console_render_queries_never_become_keys(tmp_path):
    report = tmp_path / "console.json"
    script = r'''
import asyncio, ctypes, json, msvcrt, sys, time
from pathlib import Path
from ctypes import byref, wintypes
from textual.app import App
from textual import events
from coding_agent.tui import _WindowsInlineDriver, win32

k = win32.KERNEL32
k.CreateFileW.restype = wintypes.HANDLE
input_handle = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
k.SetStdHandle(-10, wintypes.HANDLE(input_handle))
output = open("CONOUT$", "w", encoding="utf-8")
k.SetStdHandle(-11, wintypes.HANDLE(msvcrt.get_osfhandle(output.fileno())))
sys.__stdout__ = output
sys.__stdin__ = open("CONIN$", "r", encoding="utf-8")

async def run():
    driver = _WindowsInlineDriver(App())
    driver._file = output
    received = []
    driver.process_message = received.append
    driver.start_application_mode()
    try:
        for _ in range(100):
            driver.write("\x1b[2J\x1b[H\x1b[3;5H\x1b[")
            driver.write("6n\x1b[9;12H")
        driver._writer_thread.stop()
        # Real console records, fragmented across parser timeout boundaries.
        for piece in ["\x1b[>", "5u", "\x1b[?1;2c", "\x1b]10;", "rgb:ffff/ffff/ffff\x1b\\", "draft"]:
            records = (win32.INPUT_RECORD * len(piece))()
            for index, char in enumerate(piece):
                record = records[index]
                record.EventType = 1
                record.Event.KeyEvent.bKeyDown = 1
                record.Event.KeyEvent.wRepeatCount = 1
                record.Event.KeyEvent.uChar.UnicodeChar = char
            written = wintypes.DWORD()
            assert k.WriteConsoleInputW(wintypes.HANDLE(input_handle), byref(records), len(piece), byref(written))
            await asyncio.sleep(0.15)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len([e for e in received if isinstance(e, events.Key)]) >= 5:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        keys = [e for e in received if isinstance(e, events.Key)]
        Path(sys.argv[1]).write_text(json.dumps({"origin": driver.cursor_origin,
            "keys": [e.key for e in keys], "text": "".join(e.character or "" for e in keys)}))
    finally:
        driver.exit_event.set()
        driver._event_thread.join(timeout=2)
        driver._restore_console()

asyncio.run(run())
'''
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run([sys.executable, "-c", script, str(report)],
                            cwd=Path(__file__).resolve().parents[1], timeout=15,
                            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
                            capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    data = json.loads(report.read_text())
    assert data["origin"] == [4, 2]
    assert data["text"] == "draft"
    assert "escape" not in data["keys"]
