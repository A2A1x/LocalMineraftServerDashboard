"""Assert-based checks for the pure logic. Run: py test_app.py"""
import tempfile
from pathlib import Path

from app import merge_env, scan_servers


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


if __name__ == "__main__":
    test_scan_servers()
    test_scan_default_port()
    test_merge_env_preserves_token_and_comments()
    print("all tests passed")
