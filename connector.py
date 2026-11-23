#!/usr/bin/env python3
"""AIH network.connector plugin: Cloudflare Tunnel (cloudflared).

Line-delimited JSON RPC on stdin/stdout. All diagnostics go to stderr.
State lives under $HOME/.ai_home/plugin-state/network.cloudflared/.
No version gate: the binary version is reported, never enforced.
"""
import json
import re
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ID = "network.cloudflared"
PLUGIN_VERSION = "0.1.5"
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
    public_url = str(state.get("publicUrl") or "").strip()
    if not installed:
        message = "未找到 cloudflared，可执行 install 通过 Homebrew 安装"
    elif running and public_url:
        message = "公网地址：" + public_url
    elif not state.get("config"):
        message = "尚未配置 Tunnel Token、配置文件或 Quick Tunnel"
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
                        "serviceUrl": {"type": "string", "description": "Quick Tunnel 回源地址，默认 http://127.0.0.1:9527"},
                        "quickTunnel": {"type": "string", "description": "填 true 使用免账号 Quick Tunnel"},
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
        for key in ("tunnelToken", "credentialsFile", "hostname", "serviceUrl", "quickTunnel", "proxyUrl"):
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


HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251})$")


def named_config(state, credentials):
    """把 credentials JSON + hostname/serviceUrl 生成 cloudflared config.yml。

    `credentialsFile` 指向 credentials JSON（AccountTag/TunnelID/TunnelSecret）时，
    cloudflared 需要的是带 ingress 的 config.yml，而不是该 JSON 本身。也接受已经
    是 config.yml 的路径（含 `tunnel:` 字段），保持向后兼容。
    """
    config = state.get("config") or {}
    path = Path(credentials).expanduser()
    try:
        raw = path.read_text()
    except OSError as exc:
        return None, f"读取 credentialsFile 失败：{exc}"
    try:
        creds = json.loads(raw)
    except ValueError:
        creds = None
    if isinstance(creds, dict):
        tunnel_id = str(creds.get("TunnelID") or "").strip()
        if not tunnel_id:
            return None, "credentials JSON 缺少 TunnelID"
        hostname = str(config.get("hostname") or "").strip()
        if not hostname or not HOSTNAME_RE.match(hostname):
            return None, "具名隧道需要有效的 hostname"
        service = str(config.get("serviceUrl") or "http://127.0.0.1:9527").strip()
        if not (service.startswith("http://") or service.startswith("https://")):
            return None, "具名隧道需要有效的 serviceUrl"
        body = (
            f"tunnel: {tunnel_id}\n"
            f"credentials-file: {path.resolve()}\n"
            "no-autoupdate: true\n"
            "ingress:\n"
            f"  - hostname: {hostname}\n"
            f"    service: {service}\n"
            "  - service: http_status:404\n"
        )
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        generated = STATE_DIR / "config.yml"
        generated.write_text(body)
        os.chmod(generated, 0o600)
        return generated, ""
    if re.search(r"(?m)^\s*tunnel\s*:", raw):
        return path, ""
    return None, "credentialsFile 既不是 credentials JSON，也不是 cloudflared config.yml"


def enable(state):
    binary = binary_path()
    if not binary:
        return {"ok": False, "changed": False, "message": "cloudflared 尚未安装", "status": status_view(state)}
    if tunnel_running():
        state["enabled"] = True
        save_state(state)
        return {"ok": True, "changed": False, "message": "隧道已在运行", "status": status_view(state)}
    config = state.get("config") or {}
    quick = str(config.get("quickTunnel") or "").strip().lower() in ("1", "true", "yes", "on")
    token = str(config.get("tunnelToken") or "").strip()
    credentials = str(config.get("credentialsFile") or "").strip()
    if quick:
        service = str(config.get("serviceUrl") or "http://127.0.0.1:9527").strip()
        if not (service.startswith("http://") or service.startswith("https://")):
            return {"ok": False, "changed": False, "message": "Quick Tunnel 需要有效的 serviceUrl", "status": status_view(state)}
        args = [binary, "tunnel", "--no-autoupdate", "--url", service]
    else:
        if not token and not credentials:
            return {"ok": False, "changed": False, "message": "请先配置 Tunnel Token、credentialsFile，或把 quickTunnel 设为 true", "status": status_view(state)}
        args = [binary, "tunnel", "--no-autoupdate", "run"]
    env = proxy_env(state)
    if token:
        env["TUNNEL_TOKEN"] = token
    elif credentials and not quick:
        config_path, config_error = named_config(state, credentials)
        if config_path is None:
            return {"ok": False, "changed": False, "message": config_error, "status": status_view(state)}
        # --config 必须位于 run 子命令之前，否则 cloudflared 会把参数当成 run 的未知参数。
        args = [binary, "tunnel", "--no-autoupdate", "--config", str(config_path), "run"]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    log_handle = open(LOG_FILE, "wb" if quick else "ab")
    try:
        proc = subprocess.Popen(args, stdout=log_handle, stderr=log_handle, stdin=subprocess.DEVNULL,
                                start_new_session=True, env={**os.environ, **env})
    except OSError as exc:
        log_handle.close()
        return {"ok": False, "changed": False, "message": f"启动失败：{exc}"}
    log_handle.close()
    PID_FILE.write_text(str(proc.pid))
    os.chmod(PID_FILE, 0o600)
    output = []
    public_url = ""
    if quick:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                text = LOG_FILE.read_text(errors="ignore")
            except OSError:
                text = ""
            match = re.search(r"https://[A-Za-z0-9.-]+\.trycloudflare\.com", text)
            if match:
                public_url = match.group(0)
                output = command_output("", text)
                break
            time.sleep(0.5)
        if proc.poll() is not None:
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            try:
                text = LOG_FILE.read_text(errors="ignore")
            except OSError:
                text = ""
            output = command_output("", text)
            return {"ok": False, "changed": False, "message": "cloudflared 启动后立即退出，请检查配置", "output": output}
    else:
        time.sleep(1.2)
        if proc.poll() is not None:
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            try:
                text = LOG_FILE.read_text(errors="ignore")
            except OSError:
                text = ""
            return {"ok": False, "changed": False, "message": "cloudflared 启动后立即退出，请检查配置",
                    "output": command_output("", text), "status": status_view(state)}
    state["enabled"] = True
    if public_url:
        state["publicUrl"] = public_url
    save_state(state)
    if quick:
        message = ("Quick Tunnel 已启动：" + public_url) if public_url else "Quick Tunnel 已启动，尚未拿到公网地址"
    else:
        message = "隧道已启动"
    return {"ok": True, "changed": True, "message": message, "output": output, "status": status_view(state)}


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
    state["publicUrl"] = ""
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
