#!/usr/bin/env python3
"""AIH network.connector plugin: Cloudflare Tunnel (cloudflared).

Line-delimited JSON RPC on stdin/stdout. All diagnostics go to stderr.
State lives under $HOME/.ai_home/plugin-state/network.cloudflared/.
No version gate: the binary version is reported, never enforced.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ID = "network.cloudflared"
PLUGIN_VERSION = "0.1.3"
STATE_DIR = Path(os.environ.get("AIH_PLUGIN_STATE_DIR", Path.home() / ".ai_home" / "plugin-state" / PLUGIN_ID))
STATE_FILE = STATE_DIR / "state.json"
PID_FILE = STATE_DIR / "cloudflared.pid"
LOG_FILE = STATE_DIR / "cloudflared.log"

DEFAULT_STATE = {"enabled": False, "config": {}, "installedAt": 0, "updatedAt": 0}


def log(message):
    print(f"[{PLUGIN_ID}] {message}", file=sys.stderr, flush=True)


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, dict):
            merged = dict(DEFAULT_STATE)
            merged.update(data)
            if not isinstance(merged.get("config"), dict):
                merged["config"] = {}
            return merged
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_STATE)


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    state["updatedAt"] = int(time.time() * 1000)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


def proxy_env(state):
    proxy = str((state.get("config") or {}).get("proxyUrl") or "").strip()
    if not proxy:
        return {}
    return {
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "ALL_PROXY": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "all_proxy": proxy,
    }


def command_output(out, err, limit=20):
    text = (err or out or "").strip()
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def binary_path():
    return shutil.which("cloudflared") or ""


def run(cmd, timeout=20, env=None):
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def binary_version(binary):
    if not binary:
        return ""
    code, out, err = run([binary, "--version"], timeout=10)
    text = out or err
    if code != 0 or not text:
        return ""
    return text.splitlines()[0].strip()[:96]


def running_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
    except OSError:
        return 0
    return pid


def tunnel_running():
    return running_pid() > 0


def connectivity(state, installed, running):
    if not installed:
        return {"mode": "unavailable", "confidence": "observed"}
    if not running:
        return {"mode": "unavailable", "confidence": "observed"}
    return {
        "mode": "outbound-tunnel",
        "confidence": "observed",
        "observedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evidence": {"source": "cloudflared process", "path": "pid", "transport": "outbound"},
    }


def status_view(state=None):
    state = state or load_state()
    binary = binary_path()
    installed = bool(binary)
    pid = running_pid()
    running = pid > 0
    if not installed:
        label = "未安装"
    elif running:
        label = "运行中"
    elif state.get("enabled"):
        label = "已启用，未运行"
    else:
        label = "已停止"
    version = binary_version(binary)
    message = ""
    if not installed:
        message = "未找到 cloudflared，可执行 install 通过 Homebrew 安装"
    elif not state.get("config"):
        message = "尚未配置 Tunnel Token 或配置文件"
    return {
        "installed": installed,
        "enabled": bool(state.get("enabled")),
        "running": running,
        "state": label,
        "version": version,
        "message": message,
        "connectivity": connectivity(state, installed, running),
    }


def describe():
    return {
        "schemaVersion": 1,
        "kind": "describe",
        "connector": {
            "id": "cloudflared",
            "name": "Cloudflare Tunnel",
            "description": "通过 cloudflared 建立出站隧道，不声称 P2P 直连",
            "transport": "outbound",
            "operations": [
                {"id": "install", "label": "安装 cloudflared", "confirmation": "required"},
                {"id": "uninstall", "label": "卸载 cloudflared", "confirmation": "required"},
                {"id": "configure", "label": "配置 Tunnel / 代理", "inputKind": "secret", "confirmation": "required"},
                {"id": "enable", "label": "启用隧道", "confirmation": "required"},
                {"id": "disable", "label": "停用隧道", "confirmation": "required"},
                {"id": "probe", "label": "探测隧道状态", "confirmation": "none"},
            ],
            "config": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "tunnelToken": {"type": "string", "description": "Cloudflare Tunnel Token"},
                        "credentialsFile": {"type": "string", "description": "credentials JSON 路径"},
                        "hostname": {"type": "string"},
                        "serviceUrl": {"type": "string"},
                        "proxyUrl": {"type": "string", "description": "安装/连接使用的 HTTP(S) 或 SOCKS 代理"},
                    },
                },
                "secretFields": ["tunnelToken", "proxyUrl"],
            },
            "connectivityModes": ["outbound-tunnel", "unavailable"],
        },
    }


def configure(state, value):
    config = dict(state.get("config") or {})
    if isinstance(value, dict):
        if isinstance(value.get("value"), str):
            config["tunnelToken"] = value["value"].strip()
        for key in ("tunnelToken", "credentialsFile", "hostname", "serviceUrl", "proxyUrl"):
            if isinstance(value.get(key), str):
                config[key] = value[key].strip()
    elif isinstance(value, str):
        config["tunnelToken"] = value.strip()
    config = {key: val for key, val in config.items() if val}
    state["config"] = config
    save_state(state)
    changed = bool(config)
    return {"ok": True, "changed": changed, "message": "配置已保存" if changed else "配置为空", "status": status_view(state)}


def install(state):
    binary = binary_path()
    if binary:
        return {"ok": True, "changed": False, "message": "cloudflared 已安装", "status": status_view(state)}
    brew = shutil.which("brew")
    if not brew:
        return {"ok": False, "changed": False, "message": "未找到 Homebrew；请手动安装 cloudflared 后重试"}
    code, out, err = run([brew, "install", "cloudflared"], timeout=600, env=proxy_env(state))
    output = command_output(out, err)
    if code != 0:
        log(err or out)
        reason = output[-1] if output else "Homebrew 安装失败"
        return {"ok": False, "changed": False, "message": "Homebrew 安装失败：" + reason[:200], "output": output}
    return {"ok": True, "changed": True, "message": "cloudflared 安装完成", "output": output, "status": status_view(state)}


def uninstall(state):
    binary = binary_path()
    if not binary:
        return {"ok": True, "changed": False, "message": "cloudflared 未安装", "status": status_view(state)}
    brew = shutil.which("brew")
    if not brew:
        return {"ok": False, "changed": False, "message": "未找到 Homebrew；请手动卸载 cloudflared"}
    code, out, err = run([brew, "uninstall", "cloudflared"], timeout=300, env=proxy_env(state))
    output = command_output(out, err)
    if code != 0:
        log(err or out)
        reason = output[-1] if output else "Homebrew 卸载失败"
        return {"ok": False, "changed": False, "message": "Homebrew 卸载失败：" + reason[:200], "output": output}
    return {"ok": True, "changed": True, "message": "cloudflared 已卸载", "output": output, "status": status_view(state)}


def enable(state):
    if not binary_path():
        return {"ok": False, "changed": False, "message": "cloudflared 尚未安装", "status": status_view(state)}
    if tunnel_running():
        state["enabled"] = True
        save_state(state)
        return {"ok": True, "changed": False, "message": "隧道已在运行", "status": status_view(state)}
    config = state.get("config") or {}
    token = str(config.get("tunnelToken") or "").strip()
    credentials = str(config.get("credentialsFile") or "").strip()
    if not token and not credentials:
        return {"ok": False, "changed": False, "message": "请先配置 Tunnel Token 或 credentialsFile", "status": status_view(state)}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    args = [binary_path(), "tunnel", "--no-autoupdate", "run"]
    env = proxy_env(state)
    if token:
        env["TUNNEL_TOKEN"] = token
    elif credentials:
        args += ["--config", credentials]
    log_handle = open(LOG_FILE, "ab")
    try:
        proc = subprocess.Popen(args, stdout=log_handle, stderr=log_handle, stdin=subprocess.DEVNULL,
                                start_new_session=True, env={**os.environ, **env})
    except OSError as exc:
        log_handle.close()
        return {"ok": False, "changed": False, "message": f"启动失败：{exc}"}
    log_handle.close()
    PID_FILE.write_text(str(proc.pid))
    os.chmod(PID_FILE, 0o600)
    time.sleep(1.2)
    if proc.poll() is not None:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return {"ok": False, "changed": False, "message": "cloudflared 启动后立即退出，请检查配置"}
    state["enabled"] = True
    save_state(state)
    return {"ok": True, "changed": True, "message": "隧道已启动", "status": status_view(state)}


def disable(state):
    pid = running_pid()
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(20):
            if not running_pid():
                break
            time.sleep(0.1)
        if running_pid():
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    state["enabled"] = False
    save_state(state)
    return {"ok": True, "changed": bool(pid), "message": "隧道已停止" if pid else "隧道未在运行", "status": status_view(state)}


def operate(action, value):
    state = load_state()
    if action == "install":
        return install(state)
    if action == "uninstall":
        if tunnel_running():
            disable(state)
            state = load_state()
        return uninstall(state)
    if action == "configure":
        return configure(state, value)
    if action == "enable":
        return enable(state)
    if action == "disable":
        return disable(state)
    if action == "probe":
        return {"ok": True, "changed": False, "message": "探测完成", "status": status_view(state)}
    return {"ok": False, "changed": False, "message": f"未声明的操作：{action}"}


def handle_invoke(value):
    if not isinstance(value, dict):
        return describe()
    kind = value.get("kind")
    if kind == "describe":
        return describe()
    if kind == "status":
        return {"schemaVersion": 1, "kind": "status", "status": status_view()}
    if kind == "operate":
        return operate(str(value.get("action") or ""), value.get("input"))
    return describe()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if method == "invoke":
            result = handle_invoke(params.get("value"))
        elif method == "prepare":
            result = {"state": "prepared"}
        elif method == "activate":
            result = {"state": "active"}
        elif method == "dispose":
            result = {"state": "disposed"}
        elif method == "ping":
            result = {"ok": True}
        elif method == "shutdown":
            result = {"state": "disposed"}
        else:
            result = {"ok": False, "message": f"unknown method: {method}"}
        print(json.dumps({"id": request_id, "result": result}, ensure_ascii=False), flush=True)
        if method in ("dispose", "shutdown"):
            return


if __name__ == "__main__":
    main()
