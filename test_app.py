"""Assert-based checks for the pure logic. Run: py test_app.py"""
import os
import tempfile
from pathlib import Path

import socket
import struct
import threading

from app import (
    _NAME_RE,
    _is_bot_cmdline,
    _is_server_proc,
    _prune_backups,
    _read_mem,
    _set_mem,
    merge_env,
    rcon_command,
    scan_servers,
)


def test_scan_servers():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        # a real server folder
        mc = root / "My Server"
        mc.mkdir()
        (mc / "server.properties").write_text("server-port=25570\nmax-players=8\n")
        (mc / "start.bat").write_text("java -jar x.jar")
        (mc / "run.bat").write_text("java -jar x.jar")
        (mc / "server.jar").write_text("")
        # a non-server folder (no server.properties) must be ignored
        (root / "not-a-server").mkdir()
        (root / "not-a-server" / "readme.txt").write_text("hi")

        servers = scan_servers(root)
        assert len(servers) == 1, servers
        s = servers[0]
        assert s["name"] == "My Server"
        assert s["scripts"] == ["run.bat", "start.bat"], s["scripts"]
        assert s["port"] == 25570, s["port"]

    # missing root is handled, not crashing
    assert scan_servers(root / "gone") == []


def test_scan_default_port():
    with tempfile.TemporaryDirectory() as root:
        d = Path(root) / "s"
        d.mkdir()
        (d / "server.properties").write_text("motd=hi\n")  # no server-port
        assert scan_servers(root)[0]["port"] == 25565


def test_merge_env_preserves_token_and_comments():
    original = (
        "# comment stays\n"
        "DISCORD_TOKEN=SECRET.value\n"
        "CHANNEL_ID=111\n"
        "MC_HOST=127.0.0.1\n"
    )
    out = merge_env(original, {"CHANNEL_ID": "999", "POLL_INTERVAL": "15", "GUILD_ID": ""})
    lines = out.splitlines()
    assert "# comment stays" in lines
    assert "DISCORD_TOKEN=SECRET.value" in lines  # untouched
    assert "CHANNEL_ID=999" in lines               # updated in place
    assert "CHANNEL_ID=111" not in lines
    assert "POLL_INTERVAL=15" in lines             # appended (was missing)
    assert not any(l.startswith("GUILD_ID") for l in lines)  # empty -> skipped
    assert "MC_HOST=127.0.0.1" in lines


def test_is_bot_cmdline():
    assert _is_bot_cmdline(["py", "bot.py"])
    assert _is_bot_cmdline([r"C:\py\python.exe", "bot.py"])
    assert _is_bot_cmdline([r"C:\repo\BOT.PY"])
    assert not _is_bot_cmdline(["py", "app.py"])
    assert not _is_bot_cmdline([])
    assert not _is_bot_cmdline(None)


def test_is_server_proc():
    root = r"C:\Servers"
    assert _is_server_proc("java.exe", r"C:\Servers\MC", root)
    assert _is_server_proc("cmd.exe", r"c:\servers\mc", root)      # case-insensitive
    assert not _is_server_proc("java.exe", r"C:\Other\x", root)    # outside root
    assert not _is_server_proc("chrome.exe", r"C:\Servers\MC", root)  # wrong process
    assert not _is_server_proc("java.exe", "", root)               # no cwd


def test_rcon_roundtrip():
    """Fake RCON server: verifies auth + command framing against a real socket."""
    def recv_packet(c):
        raw = b""
        while len(raw) < 4:
            raw += c.recv(4 - len(raw))
        (ln,) = struct.unpack("<i", raw)
        data = b""
        while len(data) < ln:
            data += c.recv(ln - len(data))
        rid, ptype = struct.unpack("<ii", data[:8])
        return rid, ptype, data[8:-2].decode()

    def send_packet(c, rid, ptype, body):
        d = struct.pack("<ii", rid, ptype) + body.encode() + b"\x00\x00"
        c.sendall(struct.pack("<i", len(d)) + d)

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    host, port = srv.getsockname()
    result = {}

    def handle():
        c, _ = srv.accept()
        rid, ptype, body = recv_packet(c)          # auth (type 3)
        result["auth_type"] = ptype
        send_packet(c, rid if body == "secret" else -1, 2, "")  # auth response
        rid, ptype, body = recv_packet(c)          # command (type 2)
        send_packet(c, rid, 0, f"ran: {body}")
        c.close()

    threading.Thread(target=handle, daemon=True).start()
    out = rcon_command(host, port, "secret", "list", timeout=3)
    srv.close()
    assert result["auth_type"] == 3, result
    assert out == "ran: list", out


def test_valid_player_name():
    for good in ("Notch", "player_1", "A", "x" * 16):
        assert _NAME_RE.fullmatch(good), good
    for bad in ("", "has space", "semi;colon", "new\nline", "x" * 17, "quote\"x"):
        assert not _NAME_RE.fullmatch(bad), bad


def test_prune_backups():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        made = []
        for i in range(5):
            f = d / f"world-{i}.zip"
            f.write_text("x")
            os.utime(f, (i, i))  # older -> newer by mtime
            made.append(f)
        _prune_backups(d, keep=3)
        left = {p.name for p in d.glob("*.zip")}
        assert left == {"world-2.zip", "world-3.zip", "world-4.zip"}, left  # newest 3 kept


def test_idle_action():
    from app import _idle_action
    T = 15 * 60
    # unknown player count -> keep state
    assert _idle_action(None, 100.0, True, 200.0, T) == ("none", 100.0, True)
    # players online -> reset the timer
    assert _idle_action(3, 100.0, True, 200.0, T) == ("reset", None, False)
    # just went empty -> warn once, start the clock
    assert _idle_action(0, None, False, 500.0, T) == ("alert", 500.0, True)
    # still empty, under threshold -> nothing
    assert _idle_action(0, 500.0, True, 500.0 + 60, T) == ("none", 500.0, True)
    # empty past threshold -> shutdown
    assert _idle_action(0, 500.0, True, 500.0 + T, T)[0] == "shutdown"


def test_jvm_memory():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "start.bat").write_text("java -Xmx8G -Xms4G -jar server.jar nogui\npause\n")
        assert _read_mem(d) == {"xmx": "8G", "xms": "4G"}
        assert _set_mem(d, "12G", "6G") == 1
        assert _read_mem(d) == {"xmx": "12G", "xms": "6G"}
        # untouched keys when a value is blank
        assert _set_mem(d, "", "8192M") == 1
        assert _read_mem(d) == {"xmx": "12G", "xms": "8192M"}


if __name__ == "__main__":
    test_scan_servers()
    test_scan_default_port()
    test_merge_env_preserves_token_and_comments()
    test_is_bot_cmdline()
    test_is_server_proc()
    test_rcon_roundtrip()
    test_valid_player_name()
    test_prune_backups()
    test_idle_action()
    test_jvm_memory()
    print("all tests passed")
