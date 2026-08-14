"""Assert-based checks for the pure logic. Run: py test_app.py"""
import gzip
import os
import socket
import struct
import tempfile
import threading
from pathlib import Path

from app import (
    HERE,
    _NBT,
    _NAME_RE,
    _entity_scalar,
    _fmt_enchant,
    _idle_action,
    _is_bot_cmdline,
    _is_server_proc,
    _json_names,
    _keep_awake,
    _nbt_enchants,
    _nbt_items,
    _nbt_vitals,
    _parse_inventory,
    _prune_backups,
    _read_mem,
    _set_mem,
    _texture_for,
    merge_env,
    rcon_command,
    rcon_session,
    scan_servers,
)


# ---- RCON test doubles ----

def _recv_packet(c):
    raw = b""
    while len(raw) < 4:
        raw += c.recv(4 - len(raw))
    (ln,) = struct.unpack("<i", raw)
    data = b""
    while len(data) < ln:
        data += c.recv(ln - len(data))
    rid, ptype = struct.unpack("<ii", data[:8])
    return rid, ptype, data[8:-2].decode()


def _send_packet(c, rid, ptype, body):
    d = struct.pack("<ii", rid, ptype) + body.encode() + b"\x00\x00"
    c.sendall(struct.pack("<i", len(d)) + d)


def _fake_rcon(handle):
    """Bind a loopback socket, serve `handle(conn)` once in a thread, return host, port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        c, _ = srv.accept()
        try:
            handle(c)
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()


def test_scan_servers():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        mc = root / "My Server"
        mc.mkdir()
        (mc / "server.properties").write_text("server-port=25570\nmax-players=8\n")
        (mc / "start.bat").write_text("java -jar x.jar")
        (mc / "run.bat").write_text("java -jar x.jar")
        (mc / "server.jar").write_text("")
        (root / "not-a-server").mkdir()                # no server.properties -> ignored
        (root / "not-a-server" / "readme.txt").write_text("hi")

        servers = scan_servers(root)
        assert len(servers) == 1, servers
        s = servers[0]
        assert s["name"] == "My Server"
        assert s["scripts"] == ["run.bat", "start.bat"], s["scripts"]
        assert s["port"] == 25570, s["port"]

    assert scan_servers(root / "gone") == []           # missing root doesn't crash


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
    assert "DISCORD_TOKEN=SECRET.value" in lines        # untouched
    assert "CHANNEL_ID=999" in lines                    # updated in place
    assert "CHANNEL_ID=111" not in lines
    assert "POLL_INTERVAL=15" in lines                  # appended (was missing)
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
    result = {}

    def handle(c):
        rid, ptype, body = _recv_packet(c)                          # auth (type 3)
        result["auth_type"] = ptype
        _send_packet(c, rid if body == "secret" else -1, 2, "")     # auth response
        rid, ptype, body = _recv_packet(c)                          # command (type 2)
        _send_packet(c, rid, 0, f"ran: {body}")

    host, port = _fake_rcon(handle)
    out = rcon_command(host, port, "secret", "list", timeout=3)
    assert result["auth_type"] == 3, result
    assert out == "ran: list", out


def test_rcon_session():
    """One authed connection runs multiple commands; multi-packet replies reassemble
    and each command only collects its own request id."""
    def handle(c):
        rid, _pt, _b = _recv_packet(c)                 # auth
        _send_packet(c, rid, 2, "")
        while True:
            rid, _pt, body = _recv_packet(c)
            if body == "":                             # sentinel -> empty reply
                _send_packet(c, rid, 0, "")
            else:                                      # split reply across two packets
                _send_packet(c, rid, 0, "[part1]")
                _send_packet(c, rid, 0, f"[{body}]")

    host, port = _fake_rcon(handle)
    out = rcon_session(host, port, "secret", ["list", "seed"], timeout=3)
    assert out == ["[part1][list]", "[part1][seed]"], out


def test_valid_player_name():
    for good in ("Notch", "player_1", "A", "x" * 16):
        assert _NAME_RE.fullmatch(good), good
    for bad in ("", "has space", "semi;colon", "new\nline", "x" * 17, "quote\"x"):
        assert not _NAME_RE.fullmatch(bad), bad


def test_prune_backups():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for i in range(5):
            f = d / f"world-{i}.zip"
            f.write_text("x")
            os.utime(f, (i, i))                        # older -> newer by mtime
        _prune_backups(d, keep=3)
        left = {p.name for p in d.glob("*.zip")}
        assert left == {"world-2.zip", "world-3.zip", "world-4.zip"}, left  # newest 3 kept


def test_idle_action():
    T = 15 * 60
    assert _idle_action(None, 100.0, True, 200.0, T) == ("none", 100.0, True)   # unknown -> keep state
    assert _idle_action(3, 100.0, True, 200.0, T) == ("reset", None, False)     # online -> reset timer
    assert _idle_action(0, None, False, 500.0, T) == ("alert", 500.0, True)     # just emptied -> warn, start clock
    assert _idle_action(0, 500.0, True, 500.0 + 60, T) == ("none", 500.0, True)  # under threshold -> nothing
    assert _idle_action(0, 500.0, True, 500.0 + T, T)[0] == "shutdown"          # past threshold -> shutdown


def test_entity_scalar():
    assert _entity_scalar("_A2A1 has the following entity data: 20.0f") == "20.0f"
    assert _entity_scalar("P has the following entity data: [-261.45d, 65.0d, 227.36d]") == "[-261.45d, 65.0d, 227.36d]"
    assert _entity_scalar("No entity was found") is None
    assert _entity_scalar(None) is None


def test_json_names():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "ops.json").write_text('[{"name":"Notch","uuid":"x"},{"name":"Alex"}]')
        assert _json_names(d, "ops.json") == {"Notch", "Alex"}
        assert _json_names(d, "missing.json") == set()
        (d / "bad.json").write_text("not json")
        assert _json_names(d, "bad.json") == set()


def test_parse_inventory():
    snbt = ('[{Slot: 0b, id: "minecraft:diamond_sword", Count: 1b, '
            'tag: {Enchantments: [{id: "minecraft:sharpness", lvl: 5s}]}}, '
            '{Slot: 9b, id: "minecraft:dirt", Count: 64b}, '
            '{Slot: 103b, id: "minecraft:diamond_helmet", Count: 1b}]')
    items = _parse_inventory(snbt)
    got = {(it["slot"], it["id"], it["count"]) for it in items}
    assert (0, "minecraft:diamond_sword", 1) in got
    assert (9, "minecraft:dirt", 64) in got
    assert (103, "minecraft:diamond_helmet", 1) in got
    assert all("sharpness" not in it["id"] for it in items)  # nested id not captured
    assert len(items) == 3, got
    sword = next(it for it in items if it["slot"] == 0)
    assert sword["enchants"] == ["Sharpness V"]              # enchant read from nested tag
    assert "enchants" not in next(it for it in items if it["slot"] == 9)
    assert _parse_inventory("") == []


def test_enchants():
    assert _fmt_enchant("minecraft:sharpness", 5) == "Sharpness V"
    assert _fmt_enchant("minecraft:mending", 1) == "Mending"   # level omitted at 1
    assert _fmt_enchant("modid:soul_speed", 3) == "Soul Speed III"
    assert _nbt_enchants({"tag": {"Enchantments": [{"id": "minecraft:protection", "lvl": 4},
                                                    {"id": "minecraft:unbreaking", "lvl": 3}]}}) \
        == ["Protection IV", "Unbreaking III"]
    assert _nbt_enchants({"tag": {"StoredEnchantments": [{"id": "minecraft:mending", "lvl": 1}]}}) == ["Mending"]
    assert _nbt_enchants({"id": "minecraft:stick"}) == []


def test_texture_for_vanilla():
    if (HERE / "static" / "items" / "minecraft" / "diamond_sword.png").is_file():
        assert _texture_for("minecraft:diamond_sword") == "/static/items/minecraft/diamond_sword.png"


def test_render_icons():
    render = HERE / "static" / "items" / "render"
    if not (render / "stone.png").is_file():
        return  # pre-rendered 3D icons not shipped in this checkout
    assert _texture_for("minecraft:stone") == "/static/items/render/stone.png"         # 3D render preferred
    assert _texture_for("minecraft:spruce_slab") == "/static/items/render/spruce_slab.png"
    if (render / "shield.png").is_file():                                              # special renders
        assert _texture_for("minecraft:shield") == "/static/items/render/shield.png"
        assert _texture_for("minecraft:wither_skeleton_skull") == "/static/items/render/wither_skeleton_skull.png"
    if (HERE / "static" / "items" / "minecraft" / "crossbow.png").is_file():           # flat-filled item
        assert _texture_for("minecraft:crossbow") == "/static/items/minecraft/crossbow.png"
    if (HERE / "static" / "items" / "minecraft" / "apple.png").is_file():              # plain flat item
        assert _texture_for("minecraft:apple") == "/static/items/minecraft/apple.png"


def test_nbt_playerdata():
    """Round-trip a hand-built player.dat NBT through the reader + extractors."""
    def s(v):
        e = v.encode(); return struct.pack(">H", len(e)) + e

    def named(t, nm, payload):
        return struct.pack(">b", t) + s(nm) + payload

    def compound(*parts):
        return b"".join(parts) + b"\x00"

    def item(slot, iid, count):
        return compound(named(1, "Slot", struct.pack(">b", slot)),
                        named(8, "id", s(iid)),
                        named(1, "Count", struct.pack(">b", count)))

    inner = compound(
        named(5, "Health", struct.pack(">f", 19.5)),
        named(3, "foodLevel", struct.pack(">i", 17)),
        named(3, "XpLevel", struct.pack(">i", 30)),
        named(5, "XpP", struct.pack(">f", 0.5)),
        named(3, "playerGameType", struct.pack(">i", 0)),
        named(8, "Dimension", s("minecraft:the_nether")),
        named(9, "Pos", struct.pack(">b", 6) + struct.pack(">i", 3)
              + struct.pack(">ddd", 1.0, 64.0, -3.0)),
        named(9, "Inventory", struct.pack(">b", 10) + struct.pack(">i", 1) + item(0, "minecraft:stone", 64)),
        named(9, "EnderItems", struct.pack(">b", 10) + struct.pack(">i", 1) + item(3, "minecraft:diamond", 5)),
    )
    raw = struct.pack(">b", 10) + s("") + inner        # root compound, empty name

    for data in (raw, gzip.compress(raw)):             # reader accepts raw and gzip'd
        nbt = _NBT(data).parse()
        assert abs(nbt["Health"] - 19.5) < 1e-4
        v = _nbt_vitals(nbt)
        assert v["food"] == 17 and v["level"] == 30
        assert abs(v["xp_progress"] - 0.5) < 1e-4
        assert v["gamemode"] == "survival"
        assert v["dimension"] == "minecraft:the_nether"
        assert v["pos"] == {"x": 1.0, "y": 64.0, "z": -3.0}
        assert _nbt_items(nbt["Inventory"]) == [{"slot": 0, "id": "minecraft:stone", "count": 64}]
        assert _nbt_items(nbt["EnderItems"]) == [{"slot": 3, "id": "minecraft:diamond", "count": 5}]


def test_keep_awake():
    _keep_awake(True)   # sets the execution state on Windows, no-op elsewhere
    _keep_awake(False)  # must always release it, whatever the platform


def test_jvm_memory():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "start.bat").write_text("java -Xmx8G -Xms4G -jar server.jar nogui\npause\n")
        assert _read_mem(d) == {"xmx": "8G", "xms": "4G"}
        assert _set_mem(d, "12G", "6G") == 1
        assert _read_mem(d) == {"xmx": "12G", "xms": "6G"}
        assert _set_mem(d, "", "8192M") == 1                # blank value leaves that key alone
        assert _read_mem(d) == {"xmx": "12G", "xms": "8192M"}


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("all tests passed")
