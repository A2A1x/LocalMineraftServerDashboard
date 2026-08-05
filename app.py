import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

import psutil
from flask import Flask, jsonify, render_template, request
from mcstatus import JavaServer

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "servers_root": r"C:\Users\jaalf\OneDrive\Desktop\Minecraft Servers",
    "bot_dir": r"C:\Users\jaalf\Documents\Github\MinecraftServerDiscordBot",
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


CONFIG = load_config()
BOT_DIR = Path(CONFIG["bot_dir"])
SERVERS_ROOT = Path(CONFIG["servers_root"])

# Reuse the bot's tested helpers; fall back to tiny local copies if the bot
# repo isn't where config says (dashboard still works, just less DRY).
sys.path.insert(0, str(BOT_DIR))
try:
    from monitor import format_duration, is_server_up  # type: ignore
except Exception:  # pragma: no cover - only hit on a misconfigured bot_dir
    import socket

    def is_server_up(host, port, timeout=2.0):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def format_duration(seconds):
        s = int(seconds)
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        return " ".join(
            p for p in (f"{d}d" if d else "", f"{h}h" if h else "",
                        f"{m}m" if m else "", f"{s}s") if p
        ) or "0s"


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

class Proc:
    """A launched subprocess with a captured, size-capped console log."""

    def __init__(self, popen: subprocess.Popen, label: str):
        self.popen = popen
        self.label = label
        self.started_at = time.time()
        self.log = deque(maxlen=500)
        self.stopping = False
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

    def kill_tree(self):
        try:
            parent = psutil.Process(self.popen.pid)
            procs = parent.children(recursive=True) + [parent]
        except psutil.Error:
            return
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=5)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass

    def metrics(self) -> dict:
        """CPU% and RAM (MB) summed over the process tree, else zeros."""
        try:
            parent = psutil.Process(self.popen.pid)
            procs = [parent] + parent.children(recursive=True)
        except psutil.Error:
            return {"cpu": 0.0, "mem_mb": 0.0}
        cpu = 0.0
        mem = 0
        for p in procs:
            try:
                cpu += p.cpu_percent(interval=None)
                mem += p.memory_info().rss
            except psutil.Error:
                pass
        return {"cpu": round(cpu, 1), "mem_mb": round(mem / 1048576, 1)}


LOCK = threading.Lock()
SERVER: Proc | None = None
SERVER_META: dict = {}  # {name, port}
BOT: Proc | None = None


def _graceful_stop(proc: Proc, timeout: float = 90.0):
    proc.stopping = True
    proc.send("stop")
    deadline = time.time() + timeout
    while proc.alive() and time.time() < deadline:
        time.sleep(1)
    if proc.alive():
        proc.kill_tree()


# ---------- Flask ----------

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/servers")
def api_servers():
    running = SERVER_META.get("name") if SERVER and SERVER.alive() else None
    return jsonify({"servers": scan_servers(SERVERS_ROOT), "running": running})


@app.post("/api/server/start")
def api_server_start():
    global SERVER, SERVER_META
    with LOCK:
        if SERVER and SERVER.alive():
            return jsonify({"error": "A server is already running."}), 409
        data = request.get_json(force=True)
        name, script = data.get("name"), data.get("script")
        match = next((s for s in scan_servers(SERVERS_ROOT) if s["name"] == name), None)
        if not match:
            return jsonify({"error": "Unknown server."}), 404
        if script not in match["scripts"]:
            return jsonify({"error": "Unknown start script."}), 400
        folder = Path(match["path"])
        popen = subprocess.Popen(
            ["cmd", "/c", script],
            cwd=str(folder),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        SERVER = Proc(popen, name)
        SERVER_META = {"name": name, "port": match["port"]}
        return jsonify({"ok": True})


@app.post("/api/server/command")
def api_server_command():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    cmd = (request.get_json(force=True).get("cmd") or "").strip()
    if not cmd:
        return jsonify({"error": "Empty command."}), 400
    SERVER.send(cmd)
    return jsonify({"ok": True})


@app.post("/api/server/stop")
def api_server_stop():
    if not (SERVER and SERVER.alive()):
        return jsonify({"error": "No server running."}), 409
    threading.Thread(target=_graceful_stop, args=(SERVER,), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/server/status")
def api_server_status():
    if not (SERVER and SERVER.alive()):
        state = "offline"
        if SERVER and SERVER.stopping:
            state = "offline"
        return jsonify({"state": state, "log": list(SERVER.log) if SERVER else []})

    port = SERVER_META.get("port", 25565)
    host = "127.0.0.1"
    resp = {
        "state": "stopping" if SERVER.stopping else "starting",
        "name": SERVER_META.get("name"),
        "address": f"{host}:{port}",
        "uptime": format_duration(time.time() - SERVER.started_at),
        "metrics": SERVER.metrics(),
        "log": list(SERVER.log),
    }
    if not SERVER.stopping and is_server_up(host, port):
        resp["state"] = "online"
        try:
            st = JavaServer(host, port, timeout=2).status()
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
    return jsonify(resp)


def _bot_env_path() -> Path:
    return BOT_DIR / ".env"


def _bot_settings() -> dict:
    """Current bot .env values with the token masked."""
    text = ""
    try:
        text = _bot_env_path().read_text()
    except OSError:
        pass
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
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


@app.post("/api/bot/start")
def api_bot_start():
    global BOT
    with LOCK:
        if BOT and BOT.alive():
            return jsonify({"error": "Bot already running."}), 409
        if not (BOT_DIR / "bot.py").is_file():
            return jsonify({"error": f"bot.py not found in {BOT_DIR}"}), 404
        popen = subprocess.Popen(
            ["py", "bot.py"],
            cwd=str(BOT_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        BOT = Proc(popen, "discord-bot")
        return jsonify({"ok": True})


@app.post("/api/bot/stop")
def api_bot_stop():
    if not (BOT and BOT.alive()):
        return jsonify({"error": "Bot not running."}), 409
    BOT.kill_tree()
    return jsonify({"ok": True})


if __name__ == "__main__":
    url = f"http://{CONFIG['host']}:{CONFIG['port']}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=CONFIG["host"], port=CONFIG["port"], threaded=True)
