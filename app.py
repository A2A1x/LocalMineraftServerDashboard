import atexit
import ctypes
import gzip
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from collections import deque
from pathlib import Path

import psutil
from flask import Flask, Response, jsonify, render_template, request, send_file
from mcstatus import JavaServer

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "servers_root": r"C:\path\to\Minecraft Servers",
    "bot_dir": r"C:\path\to\MinecraftServerDiscordBot",
    "playit_exe": r"C:\Program Files\playit_gg\bin\playit.exe",
    "playit_log": r"C:\ProgramData\playit_gg\logs\playitd.log",
    "playit_address": "",  # the address players join
    "backup_keep": 10,  # how many world backups to retain
    "restart_time": "",  # daily restart "HH:MM" (24h); "" disables
    "auto_restart": True,  # relaunch a server if it dies unexpectedly (crash)
    "tps_alert": 12.0,  # warn when TPS drops below this; 0 disables
    "disk_alert_gb": 2.0,  # warn when free space on the servers drive drops below this
    "discord_alerts": False,  # also post alerts to the bot's Discord channel
    "idle_shutdown_min": 15,  # stop the server after this many minutes with no players; 0 disables
    "idle_grace_min": 5,  # don't start the idle clock until the server's been up this long
    "keep_awake": True,  # keep the PC awake (system sleep only) while the dashboard is open; sleeps normally once closed
    "host": "127.0.0.1",
    "port": 8765,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((HERE / "config.json").read_text()))
    except FileNotFoundError:
        pass
    return cfg


def save_config(updates: dict):
    """Merge updates into config.json (preserving other keys)."""
    try:
        cfg = json.loads((HERE / "config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg.update(updates)
    (HERE / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


CONFIG = load_config()
BOT_DIR = Path(CONFIG["bot_dir"])
SERVERS_ROOT = Path(CONFIG["servers_root"])
PLAYIT_EXE = Path(CONFIG["playit_exe"])
PLAYIT_LOG = Path(CONFIG["playit_log"])
PLAYIT_ADDRESS = CONFIG["playit_address"]
BACKUP_KEEP = int(CONFIG["backup_keep"])
RESTART_TIME = str(CONFIG["restart_time"]).strip()
AUTO_RESTART = bool(CONFIG["auto_restart"])
TPS_ALERT = float(CONFIG["tps_alert"])
DISK_ALERT_GB = float(CONFIG["disk_alert_gb"])
DISCORD_ALERTS = bool(CONFIG["discord_alerts"])
IDLE_SHUTDOWN_MIN = float(CONFIG["idle_shutdown_min"])
IDLE_GRACE_MIN = float(CONFIG["idle_grace_min"])
MC_ICONS = (HERE / "static" / "mc" / "heart_full.png").is_file()  # extracted MC heart/hunger sprites

# Launch children without flashing a console window (matters under pythonw).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ---------- RCON (Source protocol) ----------

class RconError(Exception):
    pass


def _rcon_recv(sock) -> tuple:
    def read(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf
    (length,) = struct.unpack("<i", read(4))
    data = read(length)
    req_id, ptype = struct.unpack("<ii", data[:8])
    return req_id, ptype, data[8:-2].decode("utf-8", errors="replace")


def _rcon_send(sock, ptype: int, body: str, req_id: int = 0):
    data = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(data)) + data)


def _rcon_auth(sock, password: str):
    _rcon_send(sock, 3, password)  # SERVERDATA_AUTH
    while True:  # some servers emit an empty value packet before the auth reply
        req_id, ptype, _ = _rcon_recv(sock)
        if ptype == 2:  # SERVERDATA_AUTH_RESPONSE
            if req_id == -1:
                raise RconError("authentication failed (wrong rcon.password)")
            return


def rcon_command(host: str, port: int, password: str, command: str, timeout: float = 5.0) -> str:
    """Run a single command over RCON and return the server's (first packet of) response.
    No end-of-output sentinel, so it's safe for 'stop', where the server may hang up."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _rcon_auth(sock, password)
        _rcon_send(sock, 2, command)  # SERVERDATA_EXECCOMMAND
        _, _, body = _rcon_recv(sock)
        return body


def is_server_up(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def format_duration(seconds):
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return " ".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v) or "0s"


# ---------- pure helpers (unit-tested in test_app.py) ----------

def read_properties(path: Path) -> dict:
    """Parse a server.properties file into a dict (key=value, ignore comments)."""
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def scan_servers(root) -> list:
    """Discover server folders under root: those containing server.properties.

    Returns [{name, path, scripts:[.bat names], port}] sorted by name.
    """
    root = Path(root)
    servers = []
    if not root.is_dir():
        return servers
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        props_file = d / "server.properties"
        if not props_file.is_file():
            continue
        scripts = sorted(f.name for f in d.iterdir() if f.suffix.lower() == ".bat")
        props = read_properties(props_file)
        servers.append({
            "name": d.name,
            "path": str(d),
            "scripts": scripts,
            "port": int(props.get("server-port") or 25565),
        })
    return servers


def merge_env(text: str, updates: dict) -> str:
    """Return .env text with updates applied in place, preserving unmanaged keys
    (e.g. DISCORD_TOKEN) and comments. Missing keys are appended."""
    updates = {k: str(v) for k, v in updates.items() if v is not None and v != ""}
    seen = set()
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out_lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            out_lines.append(f"{key}={val}")
    return "\n".join(out_lines) + "\n"


# ---------- managed processes ----------

def _kill(procs, timeout: float = 5):
    """Terminate procs, then kill any still alive after timeout."""
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass


class _Managed:
    """Shared state/metrics for a process tree, keyed by self.pid."""

    adopted = False

    def __init__(self, pid: int, label: str):
        self.pid = pid
        self.label = label
        self.started_at = time.time()
        self.log = deque(maxlen=500)
        self.stopping = False
        self._pcache = {}  # pid -> psutil.Process, kept across polls for cpu deltas
        self._io_prev = None  # (read_bytes, write_bytes, time) for disk I/O rate

    def kill_tree(self):
        try:
            parent = psutil.Process(self.pid)
            _kill(parent.children(recursive=True) + [parent])
        except psutil.Error:
            return

    def metrics(self) -> dict:
        """Perf stats over the process tree: CPU% (machine-wide), RAM (MB), JVM
        thread count, and disk read/write rate (MB/s).

        cpu_percent(interval=None) and I/O are deltas since the previous call, so
        we cache Process objects by pid across polls; the first poll after a
        process appears reads 0 until the next sample.
        """
        empty = {"cpu": 0.0, "mem_mb": 0.0, "threads": 0, "disk_read": 0.0, "disk_write": 0.0}
        try:
            parent = psutil.Process(self.pid)
            procs = {parent.pid: parent}
            for c in parent.children(recursive=True):
                procs[c.pid] = c
        except psutil.Error:
            return empty
        cpu = mem = threads = rbytes = wbytes = 0
        cache = {}
        for pid, proc in procs.items():
            p = self._pcache.get(pid, proc)  # reuse to keep the cpu/io baseline
            try:
                cpu += p.cpu_percent(interval=None)
                mem += p.memory_info().rss
                threads += p.num_threads()
            except psutil.Error:
                continue
            try:
                io = p.io_counters()
                rbytes += io.read_bytes
                wbytes += io.write_bytes
            except (psutil.Error, AttributeError):
                pass
            cache[pid] = p
        self._pcache = cache
        now = time.time()
        dr = dw = 0.0
        if self._io_prev:
            pr, pw, pt = self._io_prev
            dt = now - pt
            if dt > 0:
                dr = max(0.0, (rbytes - pr) / dt / 1048576)
                dw = max(0.0, (wbytes - pw) / dt / 1048576)
        self._io_prev = (rbytes, wbytes, now)
        ncpu = psutil.cpu_count() or 1
        return {"cpu": round(cpu / ncpu, 1), "mem_mb": round(mem / 1048576, 1),
                "threads": threads, "disk_read": round(dr, 2), "disk_write": round(dw, 2)}


class Proc(_Managed):
    """A subprocess this dashboard launched, with a captured console log + stdin."""

    def __init__(self, popen: subprocess.Popen, label: str):
        super().__init__(popen.pid, label)
        self.popen = popen
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        assert self.popen.stdout is not None
        for line in self.popen.stdout:
            self.log.append(line.rstrip("\n"))

    def alive(self) -> bool:
        return self.popen.poll() is None

    def send(self, cmd: str):
        if self.alive() and self.popen.stdin:
            self.popen.stdin.write(cmd + "\n")
            self.popen.stdin.flush()


class AdoptedProc(_Managed):
    """A process the dashboard reconnected to on reopen (it didn't start it). We
    can read status/metrics and terminate it, but have no stdin/stdout, so the
    console log and console commands are unavailable and stop is a terminate."""

    adopted = True

    def __init__(self, pid: int, label: str):
        super().__init__(pid, label)
        try:
            self.started_at = psutil.Process(pid).create_time()
        except psutil.Error:
            pass
        self.log.append("(reconnected to an already-running process — "
                        "live console and commands aren't available)")

    def alive(self) -> bool:
        try:
            p = psutil.Process(self.pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def send(self, cmd: str):
        pass  # no stdin on an adopted process


LOCK = threading.Lock()
SERVER: _Managed | None = None
SERVER_META: dict = {}  # {name, port}
BOT: _Managed | None = None


def _graceful_stop(proc: _Managed, timeout: float = 90.0):
    proc.stopping = True
    sent = True
    if proc.adopted:  # no stdin: clean save via RCON if available; else terminate
        rc = _server_rcon()
        try:
            if rc:
                rcon_command(*rc, "stop")
        except (OSError, RconError):
            rc = None
        sent = bool(rc)
    else:
        proc.send("stop")
    deadline = time.time() + (timeout if sent else 0)
    while proc.alive() and time.time() < deadline:
        time.sleep(1)
    if proc.alive():
        proc.kill_tree()
    _playit_run("stop")  # the tunnel follows the server down


# ---------- Flask ----------

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/servers")
def api_servers():
    running = SERVER_META.get("name") if SERVER and SERVER.alive() else None
    return jsonify({"servers": scan_servers(SERVERS_ROOT), "running": running})


@app.get("/api/settings")
def api_get_settings():
    return jsonify({"servers_root": str(SERVERS_ROOT), "bot_dir": str(BOT_DIR)})


@app.post("/api/settings")
def api_set_settings():
    global SERVERS_ROOT
    root = (request.get_json(force=True).get("servers_root") or "").strip()
    if not root:
        return jsonify({"error": "Empty path."}), 400
    p = Path(root)
    if not p.is_dir():
        return jsonify({"error": f"Not a folder: {root}"}), 400
    SERVERS_ROOT = p               # live: subsequent scans use the new folder
    save_config({"servers_root": str(p)})
    return jsonify({"ok": True, "servers_root": str(p), "count": len(scan_servers(p))})


@app.post("/api/pick-folder")
def api_pick_folder():
    """Open the native folder picker (desktop app only)."""
    try:
        import webview
        if not webview.windows:
            return jsonify({"error": "Folder picker is only available in the desktop app; "
                            "type the path instead."}), 400
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        return jsonify({"ok": True, "path": (result[0] if result else None)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _is_server_proc(name: str, cwd: str, root: str) -> bool:
    """True if a process looks like a Minecraft server (java/launcher) whose
    working dir is inside the servers root."""
    if not cwd:
        return False
    return (name.lower() in ("java.exe", "javaw.exe", "cmd.exe")
            and cwd.lower().startswith(root.lower()))


def _kill_existing_servers() -> int:
    """Ensure only one server runs. The server we manage is stopped gracefully
    (sends 'stop' so the world saves); stray, untrackable ones can only be
    terminated. Returns how many were stopped/killed."""
    n = 0
    if SERVER and SERVER.alive():
        _graceful_stop(SERVER, timeout=90)  # protects the world of our server
        n += 1
    root = str(SERVERS_ROOT)
    victims = []
    for proc in psutil.process_iter(["name", "cwd"]):
        try:
            if _is_server_proc(proc.info["name"] or "", proc.info["cwd"] or "", root):
                victims.append(proc)
        except psutil.Error:
            continue
    _kill(victims, timeout=8)
    return n + len(victims)


STATE_FILE = HERE / "state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _remember_server(name: str, script: str):
    try:
        STATE_FILE.write_text(json.dumps({"last_server": name, "last_script": script}))
    except OSError:
        pass


def _launch_server(match: dict, script: str) -> int:
    """Launch a server (killing any existing one first). Returns replaced count."""
    global SERVER, SERVER_META, SERVER_TPS, _empty_since, _idle_alerted
    SERVER_TPS = {}  # drop the previous server's TPS
    HISTORY.clear()  # drop the previous server's history
    _empty_since, _idle_alerted = None, False  # reset idle timer for the new server
    killed = _kill_existing_servers()  # guard: only one server at a time
    folder = Path(match["path"])
    popen = subprocess.Popen(
        ["cmd", "/c", script],
        cwd=str(folder),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=NO_WINDOW,
    )
    SERVER = Proc(popen, match["name"])
    SERVER_META = _server_meta(folder)
    _remember_server(match["name"], script)  # for Start All next time
    _playit_run("start")  # the tunnel comes up with the server
    return killed


def _launch_last(match: dict) -> int:
    """Launch a server with the last-used start script (else its first one)."""
    script = _load_state().get("last_script")
    return _launch_server(match, script if script in match["scripts"] else match["scripts"][0])


def _server_meta(folder: Path) -> dict:
    props = read_properties(folder / "server.properties")
    port = int(props.get("server-port") or 25565)
    return {"name": folder.name, "port": port, "folder": str(folder),
            # Query (GS4) gives the full player list; the status sample is
            # capped/anonymized. Only used when the server enables it.
            "query": props.get("enable-query", "").lower() == "true",
            "query_port": int(props.get("query.port") or port)}


@app.post("/api/server/start")
def api_server_start():
    with LOCK:
        data = request.get_json(force=True)
        name, script = data.get("name"), data.get("script")
        match = next((s for s in scan_servers(SERVERS_ROOT) if s["name"] == name), None)
        if not match:
            return jsonify({"error": "Unknown server."}), 404
        if script not in match["scripts"]:
            return jsonify({"error": "Unknown start script."}), 400
        return jsonify({"ok": True, "replaced": _launch_server(match, script)})


def _server_rcon():
    """(host, port, password) if the current server has RCON enabled, else None.
    Read from server.properties each time so config changes are picked up."""
    folder = SERVER_META.get("folder")
    if not folder:
        return None
    props = read_properties(Path(folder) / "server.properties")
    if props.get("enable-rcon", "").lower() != "true":
        return None
    pw = props.get("rcon.password", "")
    if not pw:
        return None
    return ("127.0.0.1", int(props.get("rcon.port") or 25575), pw)


def _send_command(cmd: str):
    """Dispatch a console command to the running server. Returns (ok, output)."""
    if SERVER.adopted:  # no pipe; use RCON
        rc = _server_rcon()
        if not rc:
            return False, ("Enable RCON (enable-rcon=true + rcon.password) and restart "
                           "the server to send commands to a reconnected server.")
        try:
            return True, rcon_command(*rc, cmd)
        except (OSError, RconError) as e:
            return False, f"RCON: {e}"
    SERVER.send(cmd)
    return True, ""


@app.post("/api/server/start-last")
def api_server_start_last():
    """Start the last-used server (+ ensure playit). Does NOT touch the bot — used
    by the bot's /startserver approval flow, which must not restart itself."""
    with LOCK:
        if SERVER and SERVER.alive():
            return jsonify({"ok": True, "server": SERVER_META.get("name"), "already_running": True})
        match = _server_by_name(_load_state().get("last_server"))
        if not (match and match["scripts"]):
            return jsonify({"error": "No last-used server to start — start one from the dashboard first."}), 409
        _launch_last(match)
        name = match["name"]
    _playit_run("start")  # idempotent; make sure the tunnel is up
    return jsonify({"ok": True, "server": name})


@app.post("/api/server/command")
def api_server_command():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    cmd = (request.get_json(force=True).get("cmd") or "").strip()
    if not cmd:
        return jsonify({"error": "Empty command."}), 400
    ok, out = _send_command(cmd)
    return (jsonify({"ok": True, "output": out}) if ok else (jsonify({"error": out}), 502))


_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,16}")
PLAYER_ACTIONS = {  # simple "<cmd> <name>" commands
    "kick": "kick", "ban": "ban", "pardon": "pardon", "op": "op", "deop": "deop",
    "whitelist_add": "whitelist add",
    "whitelist_remove": "whitelist remove", "kill": "kill",
}
PLAYER_EFFECTS = {  # need the player online
    "heal": "effect give {n} minecraft:instant_health 1 100 true",
    "feed": "effect give {n} minecraft:saturation 1 20 true",
    "starve": "effect give {n} minecraft:hunger 30 100 true",
}
GAMEMODES = {"0": "survival", "1": "creative", "2": "adventure", "3": "spectator"}


def _entity_scalar(out):
    """Value from 'X has the following entity data: <val>', else None."""
    if not out:
        return None
    m = re.search(r"entity data:\s*(.*)$", out, re.S)
    return m.group(1).strip() if m else None


def _current_server_folder():
    """Folder of the running server, else the last-used one (for reading player files)."""
    if SERVER_META.get("folder"):
        return Path(SERVER_META["folder"])
    m = _server_by_name(_load_state().get("last_server"))
    return Path(m["path"]) if m else None


def _json_names(folder: Path, filename: str) -> set:
    try:
        data = json.loads((folder / filename).read_text(encoding="utf-8") or "[]")
        return {str(e.get("name", "")) for e in data if e.get("name")}
    except (OSError, ValueError):
        return set()


class _NBT:
    """Minimal reader for the (gzip'd) named binary tag format Minecraft uses for
    world/playerdata/<uuid>.dat. Returns the root compound as a plain dict."""

    def __init__(self, data: bytes):
        self.b = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        self.i = 0

    def _u(self, fmt):
        v = struct.unpack_from(fmt, self.b, self.i)
        self.i += struct.calcsize(fmt)
        return v[0]

    def _name(self):
        n = self._u(">H")
        s = self.b[self.i:self.i + n].decode("utf-8", "replace")
        self.i += n
        return s

    def _payload(self, t):
        if t == 1: return self._u(">b")
        if t == 2: return self._u(">h")
        if t == 3: return self._u(">i")
        if t == 4: return self._u(">q")
        if t == 5: return self._u(">f")
        if t == 6: return self._u(">d")
        if t == 7:                                    # byte array
            n = self._u(">i"); v = self.b[self.i:self.i + n]; self.i += n; return bytes(v)
        if t == 8: return self._name()
        if t == 9:                                    # list
            et = self._u(">b"); n = self._u(">i")
            return [self._payload(et) for _ in range(n)]
        if t == 10:                                   # compound
            d = {}
            while (tt := self._u(">b")) != 0:
                nm = self._name()                     # name before payload (evaluation order)
                d[nm] = self._payload(tt)
            return d
        if t == 11: n = self._u(">i"); return [self._u(">i") for _ in range(n)]
        if t == 12: n = self._u(">i"); return [self._u(">q") for _ in range(n)]
        raise ValueError(f"bad NBT tag {t}")

    def parse(self):
        t = self._u(">b")
        if t == 0:
            return {}
        self._name()                                  # root name (usually "")
        return self._payload(t)


def _level_name(folder: Path) -> str:
    return read_properties(folder / "server.properties").get("level-name") or "world"


def _usercache(folder: Path) -> dict:
    """{name: uuid} of every player the server has seen."""
    try:
        return {e["name"]: e.get("uuid")
                for e in json.loads((folder / "usercache.json").read_text(encoding="utf-8") or "[]")
                if e.get("name")}
    except (OSError, ValueError):
        return {}


def _player_uuid(folder: Path, name: str):
    return next((u for n, u in _usercache(folder).items() if n.lower() == name.lower()), None)


_LISTS = {"opped": "ops.json", "whitelisted": "whitelist.json", "banned": "banned-players.json"}


def _list_names(folder: Path) -> dict:
    """{flag: names} from the server's ops/whitelist/banned-players files."""
    return {k: _json_names(folder, f) for k, f in _LISTS.items()}


def _offline_playerdata(folder: Path, name: str):
    """Parse the last-saved NBT for an offline player, or None if unavailable."""
    uuid = _player_uuid(folder, name)
    if not folder or not uuid:
        return None
    dat = folder / _level_name(folder) / "playerdata" / f"{uuid}.dat"
    try:
        return _NBT(dat.read_bytes()).parse()
    except (OSError, ValueError, EOFError, struct.error, gzip.BadGzipFile):
        return None  # missing, locked, or mid-write


def _nbt_vitals(nbt: dict) -> dict:
    """Pull the same fields the online RCON path exposes out of player NBT."""
    d = {}
    for key, tag in (("health", "Health"), ("food", "foodLevel"),
                     ("level", "XpLevel"), ("xp_progress", "XpP")):
        v = nbt.get(tag)
        if isinstance(v, (int, float)):
            d[key] = v
    d["gamemode"] = GAMEMODES.get(str(nbt.get("playerGameType")))
    dim = nbt.get("Dimension")
    if isinstance(dim, str):
        d["dimension"] = dim
    pos = nbt.get("Pos")
    if isinstance(pos, list) and len(pos) >= 3:
        d["pos"] = {"x": pos[0], "y": pos[1], "z": pos[2]}
    return {k: v for k, v in d.items() if v is not None}


def _nbt_items(lst) -> list:
    """A player Inventory / EnderItems NBT list -> [{slot, id, count}]."""
    out = []
    for it in (lst or []):
        iid = it.get("id") if isinstance(it, dict) else None
        if isinstance(iid, str):
            entry = {"slot": it.get("Slot"), "id": iid, "count": it.get("Count", it.get("count", 1))}
            ench = _nbt_enchants(it)
            if ench:
                entry["enchants"] = ench
            out.append(entry)
    return out


def _online_names() -> set:
    if not (SERVER and SERVER.alive()):
        return set()
    port = SERVER_META.get("port", 25565)
    try:
        if SERVER_META.get("query"):
            q = JavaServer("127.0.0.1", SERVER_META.get("query_port", port), timeout=2).query()
            return set(q.players.list)
        st = JavaServer("127.0.0.1", port, timeout=2).status()
        return {p.name for p in (st.players.sample or [])}
    except Exception:
        return set()


@app.get("/api/players")
def api_players_list():
    folder = _current_server_folder()
    if not folder:
        return jsonify({"players": [], "banned_ips": [], "server_running": False})
    lists = _list_names(folder)
    lowered = {k: {x.lower() for x in v} for k, v in lists.items()}
    online = _online_names()
    known = _usercache(folder)
    display = {}  # lower -> display name (union of every source)
    for n in [*known, *online, *(n for v in lists.values() for n in v)]:
        display.setdefault(n.lower(), n)
    players = [{"name": disp, "uuid": known.get(disp), "online": disp in online,
                **{k: low in v for k, v in lowered.items()}}
               for low, disp in sorted(display.items())]
    bips = []
    try:
        bips = [e.get("ip") for e in json.loads((folder / "banned-ips.json").read_text(encoding="utf-8") or "[]") if e.get("ip")]
    except (OSError, ValueError):
        pass
    return jsonify({"players": players, "banned_ips": bips,
                    "server_running": bool(SERVER and SERVER.alive())})


@app.get("/api/server/player")
def api_player_detail():
    name = (request.args.get("name") or "").strip()
    if not _NAME_RE.fullmatch(name):
        return jsonify({"error": "Invalid player name."}), 400
    folder = _current_server_folder()
    low = name.lower()
    flags = ({k: low in {x.lower() for x in v} for k, v in _list_names(folder).items()}
             if folder else {})
    d = {"name": name, "flags": flags, "online": name in _online_names(), "icons": MC_ICONS}
    if d["online"]:
        def num(v):
            try:
                return float(re.sub(r"[^0-9.\-]", "", v))
            except (TypeError, ValueError):
                return None
        tags = ("Health", "foodLevel", "XpLevel", "XpP", "playerGameType", "Dimension", "Pos")
        res = _rcon_query_multi([f"data get entity {name} {t}" for t in tags]) or [None] * len(tags)
        health, food, level, xpp, gm, dim, pos = (_entity_scalar(r) for r in res)
        d["health"], d["food"], d["level"], d["xp_progress"] = map(num, (health, food, level, xpp))
        d["gamemode"] = GAMEMODES.get(re.sub(r"\D", "", gm or ""))
        d["dimension"] = dim.strip('"') if dim else None
        if pos:
            nums = re.findall(r"-?\d+\.?\d*", pos)
            if len(nums) >= 3:
                d["pos"] = {"x": float(nums[0]), "y": float(nums[1]), "z": float(nums[2])}
        d["has_data"] = True
    elif folder:  # offline: read the last-saved player.dat
        nbt = _offline_playerdata(folder, name)
        if nbt:
            d.update(_nbt_vitals(nbt))
            d["has_data"] = True
    return jsonify(d)


def rcon_session(host, port, password, commands, timeout=8.0):
    """Run several commands over ONE authed RCON connection, reassembling each
    multi-packet response. Returns a list of bodies aligned with `commands`.

    Per command we read its first response packet BEFORE sending an empty 'sentinel'
    command whose reply (a distinct request id) marks the end of the output. Reading
    first avoids pipelining two packets at the server: Minecraft (and some modded
    RCON servers) close the connection if a second request arrives before the first
    response is read. We only accumulate packets whose id matches the command."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _rcon_auth(sock, password)
        out = []
        for i, command in enumerate(commands):
            cmd_id, end_id = 100 + 2 * i, 101 + 2 * i
            _rcon_send(sock, 2, command, req_id=cmd_id)
            rid, _pt, chunk = _rcon_recv(sock)      # first packet before the sentinel
            body = chunk if rid == cmd_id else ""
            _rcon_send(sock, 2, "", req_id=end_id)  # sentinel marks the end of output
            while True:
                rid, _pt, chunk = _rcon_recv(sock)
                if rid == end_id:
                    break
                if rid == cmd_id:
                    body += chunk
            out.append(body)
        return out


def _snbt_top_elements(s):
    """Split an SNBT list body (no outer brackets) into top-level {..} element strings."""
    elems, depth, start, instr, esc = [], 0, None, False, False
    for i, c in enumerate(s):
        if instr:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': instr = False
            continue
        if c == '"':
            instr = True
        elif c in "{[":
            if depth == 0 and c == "{":
                start = i
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0 and start is not None and c == "}":
                elems.append(s[start:i + 1])
                start = None
    return elems


def _snbt_top_scalars(elem):
    """Top-level scalar text of an SNBT compound (nested {}/[] removed) so we read
    the item's own id/Slot/Count, not nested enchant/tag ids."""
    out, depth, instr, esc = [], 0, False, False
    for c in elem[1:-1]:
        if instr:
            out.append(c)
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': instr = False
            continue
        if c == '"':
            instr = True; out.append(c)
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        elif depth == 0:
            out.append(c)
    return "".join(out)


_ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


def _fmt_enchant(eid, lvl):
    """'minecraft:sharpness', 5 -> 'Sharpness V' (level omitted at 1, like MC's max-1 enchants)."""
    name = eid.split(":")[-1].replace("_", " ").title()
    if lvl and lvl > 1:
        return f"{name} {_ROMAN[lvl] if lvl < len(_ROMAN) else lvl}"
    return name


def _snbt_enchants(elem):
    """Formatted enchant lines from an item's SNBT compound (Enchantments / StoredEnchantments)."""
    m = re.search(r'(?:Stored)?Enchantments:\s*\[(.*?)\]', elem, re.S)
    if not m:
        return []
    return [_fmt_enchant(mm.group(1), int(mm.group(2)))
            for mm in re.finditer(r'id:\s*"([^"]+)"[^}]*?lvl:\s*(\d+)', m.group(1))]


def _nbt_enchants(it):
    """Formatted enchant lines from an item's parsed NBT (1.20.x tag, or 1.20.5+ components)."""
    tag = it.get("tag") if isinstance(it.get("tag"), dict) else {}
    lst = tag.get("Enchantments") or tag.get("StoredEnchantments")
    if not lst:
        comp = it.get("components") if isinstance(it.get("components"), dict) else {}
        e = comp.get("minecraft:enchantments") or comp.get("minecraft:stored_enchantments") or {}
        levels = e.get("levels", e) if isinstance(e, dict) else {}
        lst = [{"id": k, "lvl": v} for k, v in levels.items()] if isinstance(levels, dict) else []
    out = []
    for e in (lst or []):
        if isinstance(e, dict) and e.get("id"):
            out.append(_fmt_enchant(e["id"], int(e.get("lvl") or 1)))
    return out


def _parse_inventory(snbt):
    """Parse a player Inventory SNBT list into [{slot, id, count, enchants}]."""
    if not snbt:
        return []
    s = snbt.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    items = []
    for elem in _snbt_top_elements(s):
        top = _snbt_top_scalars(elem)
        mid = re.search(r'\bid:\s*"([^"]+)"', top)
        if not mid:
            continue
        mslot = re.search(r"\bSlot:\s*(-?\d+)", top)
        mcount = re.search(r"\b[Cc]ount:\s*(\d+)", top)
        entry = {"slot": int(mslot.group(1)) if mslot else None,
                 "id": mid.group(1),
                 "count": int(mcount.group(1)) if mcount else 1}
        ench = _snbt_enchants(elem)
        if ench:
            entry["enchants"] = ench
        items.append(entry)
    return items


_ICON_INDEX = {}  # folder -> {(ns, name): (jar, entry, kind)}
_ICON_RX = re.compile(r"^assets/([^/]+)/textures/(item|block)/(.+)\.png$")


def _icon_index(folder):
    """Map (namespace, name) -> texture inside the selected server's mod jars.
    Built once per server folder and cached (scans ~140 jars)."""
    key = str(folder)
    if key not in _ICON_INDEX:
        idx = {}
        mdir = folder / "mods"
        for j in (sorted(mdir.glob("*.jar")) if mdir.is_dir() else []):
            try:
                with zipfile.ZipFile(j) as z:
                    for n in z.namelist():
                        m = _ICON_RX.match(n)
                        if not m:
                            continue
                        ns, kind, sub = m.groups()
                        k = (ns, sub.split("/")[-1])
                        if k not in idx or (kind == "item" and idx[k][2] != "item"):
                            idx[k] = (str(j), n, kind)  # item textures win over block
            except Exception:
                pass
        _ICON_INDEX[key] = idx
    return _ICON_INDEX[key]


def _texture_for(item_id):
    ns, _, name = item_id.partition(":")
    if not name:
        ns, name = "minecraft", ns
    if ns == "minecraft" and (HERE / "static" / "items" / "render" / f"{name}.png").is_file():
        return f"/static/items/render/{name}.png"  # pre-rendered 3D block icon
    if (HERE / "static" / "items" / ns / f"{name}.png").is_file():
        return f"/static/items/{ns}/{name}.png"  # shipped vanilla flat texture
    folder = _current_server_folder()  # modded: pull from the selected server's mods
    if folder and (ns, name) in _icon_index(folder):
        return f"/api/item-icon?ns={ns}&name={name}"
    return None


@app.get("/api/item-icon")
def api_item_icon():
    ns, name = request.args.get("ns", ""), request.args.get("name", "")
    if not re.fullmatch(r"[a-z0-9_.\-]+", ns) or not re.fullmatch(r"[a-z0-9_./\-]+", name):
        return "bad request", 400
    folder = _current_server_folder()
    hit = _icon_index(folder).get((ns, name)) if folder else None
    if not hit:
        return "not found", 404
    try:
        with zipfile.ZipFile(hit[0]) as z:
            data = z.read(hit[1])
    except Exception:
        return "not found", 404
    return Response(data, mimetype="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/server/player/inventory")
def api_player_inventory():
    name = (request.args.get("name") or "").strip()
    if not _NAME_RE.fullmatch(name):
        return jsonify({"error": "Invalid player name."}), 400
    online = name in _online_names()
    if online:  # live entity over RCON — both reads share one connection
        res = _rcon_query_multi([f"data get entity {name} Inventory",
                                 f"data get entity {name} EnderItems"]) or [None, None]
        items = _parse_inventory(_entity_scalar(res[0]))
        ender = _parse_inventory(_entity_scalar(res[1]))
    else:       # last-saved player.dat
        folder = _current_server_folder()
        nbt = _offline_playerdata(folder, name) if folder else None
        if not nbt:
            return jsonify({"online": False, "items": [], "ender": []})
        items = _nbt_items(nbt.get("Inventory"))
        ender = _nbt_items(nbt.get("EnderItems"))
    for it in items + ender:
        it["texture"] = _texture_for(it["id"])
    return jsonify({"online": online, "items": items, "ender": ender})


def _rcon_query_multi(cmds):
    """Run several read-only commands on one RCON connection; None on failure.
    Retries once, since a fresh connection often succeeds after a transient hiccup."""
    rc = _server_rcon()
    if not rc:
        return None
    for attempt in (0, 1):
        try:
            return rcon_session(*rc, cmds)
        except (OSError, RconError):
            if attempt:
                return None
            time.sleep(0.15)


@app.post("/api/server/player")
def api_server_player():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    action = data.get("action")
    if not _NAME_RE.fullmatch(name):  # guard against command injection
        return jsonify({"error": "Invalid player name."}), 400
    if action in PLAYER_ACTIONS:
        cmd = f"{PLAYER_ACTIONS[action]} {name}"
    elif action in PLAYER_EFFECTS:
        cmd = PLAYER_EFFECTS[action].format(n=name)
    elif action == "gamemode":
        if data.get("mode") not in ("survival", "creative", "adventure", "spectator"):
            return jsonify({"error": "Bad gamemode."}), 400
        cmd = f"gamemode {data['mode']} {name}"
    elif action == "tp":
        try:
            x, y, z = float(data["x"]), float(data["y"]), float(data["z"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Bad coordinates."}), 400
        cmd = f"tp {name} {x:g} {y:g} {z:g}"
    else:
        return jsonify({"error": "Unknown action."}), 400
    ok, out = _send_command(cmd)
    return (jsonify({"ok": True, "output": out}) if ok else (jsonify({"error": out}), 502))


@app.post("/api/server/stop-countdown")
def api_stop_countdown():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    target = SERVER

    def worker():
        for secs, wait in ((30, 15), (15, 10), (5, 5)):
            _send_command(f"say Server stopping in {secs} seconds")
            time.sleep(wait)
        _graceful_stop(target)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


# ---------- world backups ----------

BACKUP = {"state": "idle", "file": None, "at": 0, "error": None}


def _world_dir():
    folder = SERVER_META.get("folder")
    if not folder:
        return None
    world = (Path(folder) / _level_name(Path(folder))).resolve()
    if Path(folder).resolve() not in world.parents:  # no path traversal via level-name
        return None
    return world if world.is_dir() else None


def _prune_backups(folder: Path, keep: int):
    zips = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in zips[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _zip_world(world: Path, dest: Path) -> int:
    """Zip the world into dest, skipping session.lock (held exclusively by the
    running server) and any other momentarily-locked file. Returns skip count."""
    skipped = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _dirs, files in os.walk(world):
            for name in files:
                if name == "session.lock":
                    continue
                fp = Path(root) / name
                try:
                    zf.write(fp, fp.relative_to(world.parent))
                except OSError:  # locked/removed mid-backup; skip rather than fail
                    skipped += 1
    return skipped


def _do_backup():
    global BACKUP
    folder = SERVER_META.get("folder")
    world = _world_dir()
    if not (folder and world):
        BACKUP = {"state": "error", "error": "world folder not found", "at": time.time(), "file": None}
        return
    running = bool(SERVER and SERVER.alive())
    try:
        if running:  # flush to disk and pause saves for a consistent copy
            _send_command("save-off")
            _send_command("save-all flush")
            time.sleep(3)
        backups = Path(folder) / "backups"
        backups.mkdir(exist_ok=True)
        archive = backups / f"{world.name}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        skipped = _zip_world(world, archive)
        _prune_backups(backups, BACKUP_KEEP)
        BACKUP = {"state": "done", "file": archive.name, "error": None, "skipped": skipped,
                  "size_mb": round(archive.stat().st_size / 1048576, 1), "at": time.time()}
    except Exception as e:
        BACKUP = {"state": "error", "error": str(e), "at": time.time(), "file": None}
    finally:
        if running:
            _send_command("save-on")


@app.post("/api/server/backup")
def api_server_backup():
    global BACKUP
    if BACKUP.get("state") == "running":
        return jsonify({"error": "Backup already running."}), 409
    if not SERVER_META.get("folder"):
        return jsonify({"error": "No server selected."}), 409
    BACKUP = {"state": "running", "file": None, "at": time.time(), "error": None}
    threading.Thread(target=_do_backup, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/backup")
def api_backup():
    folder = SERVER_META.get("folder")
    files = []
    if folder:
        bdir = Path(folder) / "backups"
        if bdir.is_dir():
            for z in sorted(bdir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                files.append({"name": z.name, "size_mb": round(z.stat().st_size / 1048576, 1)})
    return jsonify({"status": BACKUP, "files": files})


# ---------- per-server config (properties / JVM memory / mods) ----------

_MEM_RE = re.compile(r"-Xm([sx])(\d+[kKmMgG]?)")


def _server_by_name(name):
    return next((s for s in scan_servers(SERVERS_ROOT) if s["name"] == name), None)


def _jvm_files(folder: Path):
    files = list(folder.glob("*.bat"))
    uj = folder / "user_jvm_args.txt"
    if uj.is_file():
        files.append(uj)
    return files


def _read_mem(folder: Path) -> dict:
    xmx = xms = None
    for f in _jvm_files(folder):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _MEM_RE.finditer(text):
            if m.group(1) == "x":
                xmx = m.group(2)
            else:
                xms = m.group(2)
    return {"xmx": xmx, "xms": xms}


def _set_mem(folder: Path, xmx: str, xms: str) -> int:
    changed = 0
    for f in _jvm_files(folder):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        new = text
        if xmx:
            new = re.sub(r"-Xmx\d+[kKmMgG]?", f"-Xmx{xmx}", new)
        if xms:
            new = re.sub(r"-Xms\d+[kKmMgG]?", f"-Xms{xms}", new)
        if new != text:
            f.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def _list_mods(folder: Path) -> list:
    mdir = folder / "mods"
    return sorted(p.name for p in mdir.glob("*.jar")) if mdir.is_dir() else []


@app.get("/api/server/config")
def api_server_config():
    s = _server_by_name(request.args.get("name"))
    if not s:
        return jsonify({"error": "Unknown server."}), 404
    folder = Path(s["path"])
    running = bool(SERVER and SERVER.alive() and SERVER_META.get("name") == s["name"])
    return jsonify({"properties": read_properties(folder / "server.properties"),
                    "mem": _read_mem(folder), "mods": _list_mods(folder), "running": running})


@app.post("/api/server/properties")
def api_server_properties():
    data = request.get_json(force=True)
    s = _server_by_name(data.get("name"))
    if not s:
        return jsonify({"error": "Unknown server."}), 404
    updates = {k: str(v).replace("\n", " ").replace("\r", " ")
               for k, v in (data.get("updates") or {}).items() if k}
    path = Path(s["path"]) / "server.properties"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    path.write_text(merge_env(text, updates), encoding="utf-8")
    return jsonify({"ok": True})


@app.post("/api/server/memory")
def api_server_memory():
    data = request.get_json(force=True)
    s = _server_by_name(data.get("name"))
    if not s:
        return jsonify({"error": "Unknown server."}), 404
    xmx = (data.get("xmx") or "").strip()
    xms = (data.get("xms") or "").strip()
    for v in (xmx, xms):
        if v and not re.fullmatch(r"\d+[kKmMgG]?", v):
            return jsonify({"error": f"Invalid memory value: {v!r} (e.g. 8G or 8192M)"}), 400
    return jsonify({"ok": True, "changed": _set_mem(Path(s["path"]), xmx, xms)})


@app.get("/api/server/log/download")
def api_log_download():
    folder = SERVER_META.get("folder")
    if folder:
        log = Path(folder) / "logs" / "latest.log"
        if log.is_file():
            return send_file(str(log), as_attachment=True, download_name="latest.log")
    return jsonify({"error": "No log available."}), 404


@app.post("/api/server/stop")
def api_server_stop():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    threading.Thread(target=_graceful_stop, args=(SERVER,), daemon=True).start()
    return jsonify({"ok": True})


_BOLT = "⚡"  # spark prefixes its output with a lightning bolt
_NOISE = ("RCON Client", "RCON Listener", _BOLT, "spark-worker")  # shown as metrics, not console


def _console() -> list:
    """The server console (last 300 lines): from the captured pipe for an owned
    server or logs/latest.log for an adopted one, with RCON/spark noise removed."""
    if SERVER and SERVER.adopted:
        folder = SERVER_META.get("folder")
        raw = _tail(Path(folder) / "logs" / "latest.log", n=600) if folder else []
        if not raw:
            raw = list(SERVER.log) if SERVER else []
    else:
        raw = list(SERVER.log) if SERVER else []
    return [ln for ln in raw if not any(n in ln for n in _NOISE)][-300:]


# ---------- TPS via spark ----------

SERVER_TPS = {}  # {tps, series, mspt, at}


def _has_spark(folder) -> bool:
    return any(m.lower().startswith("spark") for m in _list_mods(Path(folder)))


def _spark_payload(line: str) -> str:
    m = re.search(re.escape("[" + _BOLT + "]") + r"\s*(.*)$", line)
    return m.group(1) if m else ""


def _parse_tps(lines) -> dict:
    """Pull the most recent TPS series and median MSPT out of spark log lines."""
    series = mspt = None
    for i, ln in enumerate(lines):
        if "TPS from last" in ln and i + 1 < len(lines):
            nums = re.findall(r"\d+\.?\d*", _spark_payload(lines[i + 1]))
            if nums:
                series = [float(x) for x in nums]
        elif "Tick durations" in ln and i + 1 < len(lines):
            nums = re.findall(r"\d+\.?\d*", _spark_payload(lines[i + 1]).split(";")[0])
            if len(nums) >= 2:
                mspt = nums[1]  # median of the last 10s
    tps = (series[2] if len(series) >= 3 else series[-1]) if series else None
    return {"tps": tps, "series": series, "mspt": float(mspt) if mspt else None}


def _refresh_tps():
    """Ask spark for TPS over RCON, then read the result from the log."""
    if not (SERVER and SERVER.alive()):
        return
    folder = SERVER_META.get("folder")
    rc = _server_rcon()
    if not (folder and rc and _has_spark(folder)):
        return
    try:
        rcon_command(*rc, "spark tps")
    except (OSError, RconError):
        return
    time.sleep(2)  # spark writes its report from a worker thread a moment later
    data = _parse_tps(_tail(Path(folder) / "logs" / "latest.log", n=60))
    if data["tps"] is not None:
        data["at"] = time.time()
        global SERVER_TPS
        SERVER_TPS = data


def _tps_worker():
    while True:
        time.sleep(20)
        try:
            _refresh_tps()
        except Exception:
            pass


@app.get("/api/server/status")
def api_server_status():
    if not (SERVER and SERVER.alive()):
        return jsonify({"state": "offline", "log": list(SERVER.log) if SERVER else []})

    port = SERVER_META.get("port", 25565)
    host = "127.0.0.1"
    resp = {
        "state": "stopping" if SERVER.stopping else "starting",
        "name": SERVER_META.get("name"),
        "address": f"{host}:{port}",
        "uptime": format_duration(time.time() - SERVER.started_at),
        "metrics": SERVER.metrics(),
        "log": _console(),
        "adopted": SERVER.adopted,
        "rcon": _server_rcon() is not None,  # config-based; no probe (avoids log spam)
    }
    vm = psutil.virtual_memory()
    resp["system"] = {"mem_pct": vm.percent,
                      "mem_used_gb": round(vm.used / 1073741824, 1),
                      "mem_total_gb": round(vm.total / 1073741824, 1)}
    if SERVER_TPS.get("tps") is not None and time.time() - SERVER_TPS.get("at", 0) < 90:
        resp["tps"] = {k: SERVER_TPS.get(k) for k in ("tps", "mspt", "series")}
    if not SERVER.stopping and is_server_up(host, port):
        resp["state"] = "online"
        got_names = False
        if SERVER_META.get("query"):  # full roster via GS4 query when enabled
            try:
                q = JavaServer(host, SERVER_META.get("query_port", port), timeout=2).query()
                resp["players"] = {"online": q.players.online, "max": q.players.max,
                                   "names": sorted(q.players.list)}
                got_names = True
            except Exception:
                pass  # query enabled but not answering; fall back to the sample
        try:
            st = JavaServer(host, port, timeout=2).status()
            if not got_names:  # status sample: capped and may be "Anonymous Player"
                resp["players"] = {
                    "online": st.players.online,
                    "max": st.players.max,
                    "names": sorted(p.name for p in (st.players.sample or [])),
                }
            resp["version"] = st.version.name
            try:
                resp["motd"] = st.motd.to_plain()
            except Exception:
                resp["motd"] = str(getattr(st, "description", ""))
        except Exception:
            pass  # port open but full ping not ready yet
    _maybe_sample(resp)
    return jsonify(resp)


def _bot_env_path() -> Path:
    return BOT_DIR / ".env"


def _bot_settings() -> dict:
    """Current bot .env values with the token masked."""
    vals = read_properties(_bot_env_path())
    token = vals.get("DISCORD_TOKEN", "")
    return {
        "DISCORD_TOKEN_set": bool(token),
        "CHANNEL_ID": vals.get("CHANNEL_ID", ""),
        "MC_HOST": vals.get("MC_HOST", "127.0.0.1"),
        "MC_PORT": vals.get("MC_PORT", "25565"),
        "POLL_INTERVAL": vals.get("POLL_INTERVAL", "30"),
        "GUILD_ID": vals.get("GUILD_ID", ""),
    }


@app.get("/api/bot")
def api_bot():
    return jsonify({
        "running": bool(BOT and BOT.alive()),
        "settings": _bot_settings(),
        "log": list(BOT.log) if BOT else [],
    })


@app.post("/api/bot/settings")
def api_bot_settings():
    data = request.get_json(force=True)
    keys = ("CHANNEL_ID", "MC_HOST", "MC_PORT", "POLL_INTERVAL", "GUILD_ID")
    updates = {k: data.get(k) for k in keys}
    path = _bot_env_path()
    try:
        text = path.read_text()
    except OSError:
        text = ""
    path.write_text(merge_env(text, updates))
    return jsonify({"ok": True, "settings": _bot_settings()})


def _is_bot_cmdline(cmdline) -> bool:
    """True if a process command line looks like it's running the bot script."""
    return any("bot.py" in str(a).lower() for a in (cmdline or []))


def _kill_existing_bots() -> int:
    """Kill any stray bot.py processes (launcher + child) under BOT_DIR so we
    never end up with two bots on the same token. Returns how many were killed."""
    target = str(BOT_DIR).lower()
    victims = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if not _is_bot_cmdline(proc.info["cmdline"]):
                continue
            try:
                if proc.cwd().lower() != target:
                    continue
            except (psutil.Error, OSError):
                pass  # cwd unreadable; still a bot.py match, kill it
            victims.append(proc)
        except psutil.Error:
            continue
    _kill(victims)
    return len(victims)


def _launch_bot() -> int:
    """Launch the bot (killing any existing one first). Returns replaced count."""
    global BOT
    killed = _kill_existing_bots()  # guard: no duplicate bots
    popen = subprocess.Popen(
        ["py", "bot.py"],
        cwd=str(BOT_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=NO_WINDOW,
    )
    BOT = Proc(popen, "discord-bot")
    return killed


@app.post("/api/bot/start")
def api_bot_start():
    with LOCK:
        if not (BOT_DIR / "bot.py").is_file():
            return jsonify({"error": f"bot.py not found in {BOT_DIR}"}), 404
        return jsonify({"ok": True, "replaced": _launch_bot()})


@app.post("/api/bot/stop")
def api_bot_stop():
    if not (BOT and BOT.alive()):
        return jsonify({"error": "Bot not running."}), 409
    BOT.kill_tree()
    return jsonify({"ok": True})


# ---------- playit.gg ----------

def _tail(path: Path, n: int = 300, chunk: int = 64000) -> list:
    """Last n lines of a (possibly large) log file, reading only its tail."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > chunk and lines:
        lines = lines[1:]  # first line is likely partial
    return lines[-n:]


def _playit_run(*args, timeout=30) -> dict:
    if not PLAYIT_EXE.is_file():
        return {"error": f"playit not found at {PLAYIT_EXE}"}
    try:
        r = subprocess.run([str(PLAYIT_EXE), *args], capture_output=True,
                           text=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip(),
            "raw": r.stdout}


def playit_status() -> dict:
    r = _playit_run("status", timeout=10)
    if "error" in r:
        return {"running": False, "error": r["error"]}
    info = {}
    for line in r["raw"].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
    phase = info.get("Phase", "")
    out = {"running": phase == "running", "phase": phase or "unknown",
           "version": info.get("Version", ""),
           "secret_configured": info.get("Secret configured", "") == "true"}
    up = info.get("Uptime", "").split()
    if up and up[0].isdigit():
        out["uptime"] = format_duration(int(up[0]))
    return out


@app.get("/api/playit")
def api_playit():
    return jsonify({"status": playit_status(), "log": _tail(PLAYIT_LOG), "address": PLAYIT_ADDRESS})


@app.post("/api/playit/start")
def api_playit_start():
    return jsonify(_playit_run("start"))


@app.post("/api/playit/stop")
def api_playit_stop():
    return jsonify(_playit_run("stop"))


@app.post("/api/start-all")
def api_start_all():
    """Start everything: playit tunnel, Discord bot, and the last-used server."""
    out = {"playit": "started" if _playit_run("start").get("ok") else "unavailable"}
    with LOCK:
        if BOT and BOT.alive():
            out["bot"] = "already running"
        elif (BOT_DIR / "bot.py").is_file():
            _launch_bot()
            out["bot"] = "started"
        else:
            out["bot"] = "bot.py not found"

        if SERVER and SERVER.alive():
            out["server"] = f"{SERVER_META.get('name')} already running"
        else:
            name = _load_state().get("last_server")
            match = _server_by_name(name)
            if match and match["scripts"]:
                _launch_last(match)
                out["server"] = f"started {name}"
            elif name:
                out["server"] = f"last server '{name}' not found"
            else:
                out["server"] = "no server — pick one on the MC Server tab"
    return jsonify({"ok": True, "result": out})


def _stop_async(*fns):
    """Run the stop steps in the background, each independently of the others' failures."""
    if SERVER and SERVER.alive():
        SERVER.stopping = True  # immediate UI feedback; worker does the real stop

    def worker():
        for fn in fns:
            try:
                fn()
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/stop-all")
def api_stop_all():
    """Stop everything: MC server (gracefully), Discord bot, and playit tunnel."""
    return _stop_async(_kill_existing_servers, _kill_existing_bots, lambda: _playit_run("stop"))


@app.post("/api/stop-server-playit")
def api_stop_server_playit():
    """Stop the MC server (gracefully) and the playit tunnel; leave the bot running."""
    return _stop_async(_kill_existing_servers, lambda: _playit_run("stop"))


def _ensure_playit():
    """On launch, match the tunnel to the server: up if one is running, else down."""
    if PLAYIT_EXE.is_file():
        _playit_run("start" if (SERVER and SERVER.alive()) else "stop")


# ---------- history, alerts, supervisor ----------

HISTORY = deque(maxlen=240)   # ~40 min at 10s: {t, cpu, mem_mb, players, tps}
ALERTS = deque(maxlen=50)     # {text, level, at}
_alert_state = {}             # dedup: only alert on the falling edge
_last_sample = [0.0]
_restart_fired = None
_last_autorestart = 0.0


def _discord_notify(text: str, level: str = "warn"):
    """Post an alert to the bot's channel as the bot (REST API, bot token)."""
    try:
        env = read_properties(_bot_env_path())
        token, ch = env.get("DISCORD_TOKEN"), env.get("CHANNEL_ID")
        if not (token and ch):
            return
        color = 0xED4245 if level == "error" else 0xFEE75C  # brand red / yellow
        payload = {"embeds": [{"title": "⚠️ Server Alert", "description": text, "color": color}]}
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{ch}/messages",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                     "User-Agent": "mc-dashboard"}, method="POST")
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def _alert(text: str, level: str = "warn"):
    ALERTS.append({"text": text, "level": level, "at": time.time()})
    if DISCORD_ALERTS:
        threading.Thread(target=_discord_notify, args=(text, level), daemon=True).start()


def _maybe_sample(resp: dict):
    """Append a history point (throttled) from an already-computed status resp."""
    if resp.get("state") not in ("online", "starting"):
        return
    now = time.time()
    if now - _last_sample[0] < 10:
        return
    _last_sample[0] = now
    m = resp.get("metrics") or {}
    HISTORY.append({"t": now, "cpu": m.get("cpu", 0), "mem_mb": m.get("mem_mb", 0),
                    "players": (resp.get("players") or {}).get("online", 0),
                    "tps": (resp.get("tps") or {}).get("tps")})


def _restart_with_countdown():
    name = SERVER_META.get("name")
    match = _server_by_name(name)
    for secs, wait in ((60, 30), (30, 20), (10, 10)):
        _send_command(f"say Scheduled restart in {secs} seconds")
        time.sleep(wait)
    if SERVER:
        _graceful_stop(SERVER)
    if match and match["scripts"]:
        _launch_last(match)
        _alert(f"Scheduled restart of '{name}'", "info")


def _supervisor():
    """Auto-restart a crashed server and run the daily scheduled restart."""
    global _restart_fired, _last_autorestart
    while True:
        time.sleep(15)
        try:
            s = SERVER
            if (AUTO_RESTART and s and not s.alive() and not s.stopping
                    and not getattr(s, "_crash_handled", False)):
                s._crash_handled = True
                name = SERVER_META.get("name")
                match = _server_by_name(name)
                if time.time() - _last_autorestart < 30:
                    _alert(f"'{name}' died again too soon — not auto-restarting", "error")
                elif match and match["scripts"]:
                    _last_autorestart = time.time()
                    _alert(f"'{name}' crashed — auto-restarting", "error")
                    _launch_last(match)
            if RESTART_TIME and SERVER and SERVER.alive():
                key = time.strftime("%Y-%m-%d %H:%M")
                if time.strftime("%H:%M") == RESTART_TIME and _restart_fired != key:
                    _restart_fired = key
                    threading.Thread(target=_restart_with_countdown, daemon=True).start()
        except Exception:
            pass


def _alerts_worker():
    while True:
        time.sleep(30)
        try:
            if TPS_ALERT and SERVER and SERVER.alive():
                fresh = time.time() - SERVER_TPS.get("at", 0) < 120
                tps = SERVER_TPS.get("tps") if fresh else None
                low = tps is not None and tps < TPS_ALERT
                if low and not _alert_state.get("tps_low"):
                    _alert(f"Low TPS: {tps} (below {TPS_ALERT})")
                _alert_state["tps_low"] = low
            if DISK_ALERT_GB:
                free = psutil.disk_usage(str(SERVERS_ROOT)).free / 1073741824
                low = free < DISK_ALERT_GB
                if low and not _alert_state.get("disk_low"):
                    _alert(f"Low disk space: {free:.1f} GB free on the servers drive", "error")
                _alert_state["disk_low"] = low
        except Exception:
            pass


# ---------- idle auto-shutdown ----------

_empty_since = None   # timestamp the running server first had 0 players
_idle_alerted = False


def _online_count():
    """Online player count of the running server, or None if it can't be read."""
    if not (SERVER and SERVER.alive()):
        return None
    try:
        return JavaServer("127.0.0.1", SERVER_META.get("port", 25565), timeout=3).status().players.online
    except Exception:
        return None


def _idle_action(online, empty_since, alerted, now, threshold_s):
    """Decide what the idle watcher should do. Pure -> unit-testable.
    Returns (action, new_empty_since, new_alerted); action in
    {'none','reset','alert','shutdown'}."""
    if online is None:
        return ("none", empty_since, alerted)     # unknown; keep state
    if online > 0:
        return ("reset", None, False)             # someone's on; cancel any timer
    if empty_since is None:
        return ("alert", now, True)               # just went empty -> warn once
    if now - empty_since >= threshold_s:
        return ("shutdown", None, False)
    return ("none", empty_since, alerted)


def _idle_tick(now=None):
    """One idle-watch cycle; performs the decided action. Returns the action."""
    global _empty_since, _idle_alerted
    now = now if now is not None else time.time()
    if not IDLE_SHUTDOWN_MIN or not (SERVER and SERVER.alive()) or SERVER.stopping:
        _empty_since, _idle_alerted = None, False
        return "inactive"
    if now - SERVER.started_at < IDLE_GRACE_MIN * 60:  # grace period after startup
        _empty_since, _idle_alerted = None, False
        return "grace"
    action, _empty_since, _idle_alerted = _idle_action(
        _online_count(), _empty_since, _idle_alerted, now, IDLE_SHUTDOWN_MIN * 60)
    if action == "alert":
        _alert(f"No players online - the server will shut down in "
               f"{IDLE_SHUTDOWN_MIN:g} min if it stays empty")
    elif action == "shutdown":
        _alert(f"No players for {IDLE_SHUTDOWN_MIN:g} min - shutting the server down")
        _graceful_stop(SERVER)
    return action


def _idle_watch():
    while True:
        time.sleep(30)
        try:
            _idle_tick()
        except Exception:
            pass


@app.get("/api/history")
def api_history():
    return jsonify({"history": list(HISTORY)})


@app.get("/api/alerts")
def api_alerts():
    return jsonify({"alerts": list(ALERTS)})


def _adopt_running():
    """On reopen, reconnect to an already-running server/bot so the dashboard
    reflects and can control them. Adopted processes have no console/stdin."""
    global SERVER, SERVER_META, BOT
    if not (SERVER and SERVER.alive()):
        by_folder = {}
        for pr in psutil.process_iter(["name", "cwd", "create_time"]):
            try:
                if _is_server_proc(pr.info["name"] or "", pr.info["cwd"] or "", str(SERVERS_ROOT)):
                    by_folder.setdefault(Path(pr.info["cwd"]), []).append(pr)
            except psutil.Error:
                continue
        if by_folder:
            folder = max(by_folder,  # the most-recently-started server folder
                         key=lambda f: max(p.info["create_time"] for p in by_folder[f]))
            procs = by_folder[folder]
            anchor = next((p for p in procs if (p.info["name"] or "").lower() == "cmd.exe"), procs[0])
            SERVER = AdoptedProc(anchor.pid, folder.name)
            SERVER_META = _server_meta(folder)
    if not (BOT and BOT.alive()):
        best = None
        for pr in psutil.process_iter(["name", "cmdline"]):
            try:
                if not _is_bot_cmdline(pr.info["cmdline"]):
                    continue
                try:
                    if pr.cwd().lower() != str(BOT_DIR).lower():
                        continue
                except (psutil.Error, OSError):
                    pass
                if best is None or (pr.info["name"] or "").lower() == "python.exe":
                    best = pr  # prefer the real interpreter over the py.exe launcher
            except psutil.Error:
                continue
        if best:
            BOT = AdoptedProc(best.pid, "discord-bot")
    # playit is a Windows service; playit_status() already reflects it.


# ---------- entrypoints ----------

def _keep_awake(enable=True):
    """Stop the machine sleeping while the dashboard is open so the server/tunnel
    stay reachable. Prevents system sleep only (the display may still turn off);
    Windows-only, no-op elsewhere. Sleep behaviour returns to normal on exit."""
    if os.name != "nt":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0))
    except Exception:
        pass


def _serve():
    app.run(host=CONFIG["host"], port=CONFIG["port"], threaded=True, use_reloader=False)


def run_web():
    url = f"http://{CONFIG['host']}:{CONFIG['port']}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    _serve()


def run_desktop():
    import webview
    url = f"http://{CONFIG['host']}:{CONFIG['port']}"
    threading.Thread(target=_serve, daemon=True).start()
    for _ in range(100):  # wait up to ~10s for Flask to accept connections
        if is_server_up(CONFIG["host"], CONFIG["port"], timeout=0.2):
            break
        time.sleep(0.1)
    webview.create_window("Minecraft Dashboard", url, width=1320, height=900,
                          confirm_close=True)
    webview.start()


if __name__ == "__main__":
    if CONFIG.get("keep_awake", True):  # runs on the main thread, which lives for the app's lifetime
        _keep_awake(True)
        atexit.register(_keep_awake, False)
    _adopt_running()  # reconnect to any server/bot already running
    threading.Thread(target=_ensure_playit, daemon=True).start()
    threading.Thread(target=_tps_worker, daemon=True).start()  # spark TPS polling
    threading.Thread(target=_supervisor, daemon=True).start()  # crash/scheduled restart
    threading.Thread(target=_alerts_worker, daemon=True).start()  # TPS/disk alerts
    threading.Thread(target=_idle_watch, daemon=True).start()  # idle auto-shutdown
    if "--web" in sys.argv:
        run_web()
    else:
        try:
            run_desktop()
        except ImportError:
            print("pywebview not installed; opening in browser instead. "
                  "Install it with: py -m pip install pywebview")
            run_web()
