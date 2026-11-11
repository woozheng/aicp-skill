"""WSL 通道 — 从 Windows 宿主驱动 WSL 发行版（执行命令 / 读写文件 / 查看环境）

约定：
- 仅在 Windows 宿主上可用，非 Windows 直接返回 ok=False + error。
- 统一走 wsl.exe -d <distro> -e bash -lc，不依赖 WSL 内是否装了 sshd。
- 所有 action 走 envelop.payload["action"] 分派。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import time
from pathlib import Path

DEFAULT_DISTRO = "Ubuntu"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300

_DEPLOY_SCRIPT = r"""#!/usr/bin/env bash
set -u
SRC='__SRC__'
DST='__DST__'
PORT='__PORT__'
LOGF=/root/aicp_deploy.log
exec >> "$LOGF" 2>&1
echo "=== deploy start $(date -Is) ==="
echo "SRC=$SRC DST=$DST PORT=$PORT"

SECRET_FILE=/root/.aicp_bio_secret
OLD_MODE=$(awk '/^auth:/{f=1;next} f&&/^[ \t]/{if($1=="mode:"){print $2;exit}} f&&/^[^ \t]/{exit}' "$DST/aicp.yaml" 2>/dev/null | tr -d '"[:space:]')
echo "OLD_MODE=$OLD_MODE"

cp "$DST/aicp.yaml" "$DST/aicp.yaml.predeploy" 2>/dev/null || true

systemctl stop aicp 2>/dev/null || true
pkill -f 'python -m runtime' 2>/dev/null || true
sleep 1

mkdir -p "$DST"
tar -C "$SRC" -cf - __EXCLUDES__ . | tar -C "$DST" -xf -
echo "copy done"

# ★ 跨系统路径改写：data/*.json 里可能残留 Windows 绝对路径（E:\aicp-skills\...），
#   部署到 Linux 后这些路径全部失效 → 应用里点链接就是 404。
#   这里统一改写成 Linux 路径，并把被引用到的 uploads 目录同步过来。
python3 - "$DST" "$SRC" "$PORT" <<'PY'
import json, re, shutil, sys
from pathlib import Path

dst = Path(sys.argv[1])
src = Path(sys.argv[2])
port = str(sys.argv[3])
dst_root = str(dst)
src_l = str(src).lower()
win_re = re.compile(r'^([A-Za-z]:[\\/])([^\\/]+)[\\/]')

# 源端端口（Windows 实例默认 9000）→ 目标端端口（deploy 传入）
src_port = '9000'
try:
    m = re.search(r'(?m)^port:\s*(\d+)', (src / 'aicp.yaml').read_text(encoding='utf-8', errors='ignore'))
    if m:
        src_port = m.group(1)
except Exception:
    pass
dst_port = port
dst_ws_port = str(int(port) + 1)

rewritten = []
need_uploads = set()


def walk(node, hits, changed):
    if isinstance(node, str):
        s = node
        m = win_re.match(s)
        if m and m.group(2).lower() in src_l:
            root = m.group(0)[:-1]
            rest = s[len(root):].replace('\\', '/').lstrip('/')
            hits.append(rest)
            s = dst_root + '/' + rest
        if src_port != dst_port:
            s = s.replace('127.0.0.1:%s/ws' % src_port, '127.0.0.1:%s/ws' % dst_ws_port)
            s = s.replace('127.0.0.1:%s' % src_port, '127.0.0.1:%s' % dst_port)
        if s != node:
            changed[0] = True
        return s
    if isinstance(node, list):
        return [walk(x, hits, changed) for x in node]
    if isinstance(node, dict):
        return {k: walk(v, hits, changed) for k, v in node.items()}
    return node


for p in list(dst.rglob('*.json')):
    try:
        raw = p.read_text(encoding='utf-8')
    except Exception:
        continue
    if not re.search(r'[A-Za-z]:[\\/]', raw) and ('127.0.0.1:%s' % src_port) not in raw:
        continue
    try:
        data = json.loads(raw)
    except Exception:
        continue
    hits = []
    changed = [False]
    new = walk(data, hits, changed)
    if not hits and not changed[0]:
        continue
    for rest in hits:
        if rest.startswith('data/uploads/'):
            need_uploads.add(rest.split('/')[2])
    try:
        p.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding='utf-8')
        rewritten.append(str(p.relative_to(dst)))
    except Exception:
        pass

print('rewritten %d json files' % len(rewritten))
for name in sorted(need_uploads):
    s = src / 'data' / 'uploads' / name
    d = dst / 'data' / 'uploads' / name
    if s.is_dir() and not d.exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(s), str(d))
        print('synced uploads/' + name)
PY

cd "$DST"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
echo "deps done"

python3 - "$DST" "$PORT" "${OLD_MODE:-local}" <<'PY'
import sys, re, pathlib
d, port, mode = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(d) / 'aicp.yaml'
t = p.read_text(encoding='utf-8')
t = re.sub(r'(?m)^port:\s*\d+', 'port: ' + port, t)
t = re.sub(r'(?m)^(\s*external_url:\s*)ws://127\.0\.0\.1:\d+/ws',
           r'\1ws://127.0.0.1:' + str(int(port) + 1) + '/ws', t)
if mode in ('hybrid', 'token'):
    lines = t.splitlines(keepends=True)
    out = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if re.match(r'^auth:\s*$', ln):
            out.append(ln)
            found = False
            i += 1
            while i < n:
                cur = lines[i]
                if cur.strip() == '' or cur[:1] not in (' ', '\t'):
                    break
                m = re.match(r'^(\s*mode:\s*)\S+', cur)
                if m:
                    out.append(m.group(1) + mode + '\n')
                    found = True
                else:
                    out.append(cur)
                i += 1
            if not found:
                out.append('  mode: ' + mode + '\n')
            # ★ auth 段之后的全部内容必须保留。原实现在此 break，
            #   导致 visualization / models 等段被整段截断（Linux 侧 LLM 未配置）
            while i < n:
                out.append(lines[i])
                i += 1
            break
        out.append(ln)
        i += 1
    t = ''.join(out)
p.write_text(t, encoding='utf-8')
print('yaml patched, auth.mode=' + mode)
PY

cat > /etc/systemd/system/aicp.service <<UNIT
[Unit]
Description=AICP Linux (WSL)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DST
Environment=PYTHONUNBUFFERED=1
$( [ -s "$SECRET_FILE" ] && echo "Environment=BIO_SECRET=$(cat "$SECRET_FILE")" )
ExecStart=$DST/.venv/bin/python -m runtime
Restart=always
RestartSec=5
StandardOutput=append:$DST/aicp.service.log
StandardError=append:$DST/aicp.service.log

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable aicp
systemctl restart aicp
sleep 8

HEALTH_URL="http://127.0.0.1:$PORT/console.html"
OK=0
code=000
i=0
while [ $i -lt 20 ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' "$HEALTH_URL" 2>/dev/null || true)
  if [ "$code" = "200" ]; then OK=1; break; fi
  i=$((i+1))
  sleep 2
done

if [ "$OK" = "1" ]; then
  echo "health check ok (http $code)"
  systemctl is-active aicp
  echo "=== deploy done $(date -Is) ==="
else
  echo "health check FAILED (last http=$code)"
  if [ -f "$DST/aicp.yaml.predeploy" ]; then
    cp "$DST/aicp.yaml.predeploy" "$DST/aicp.yaml"
    echo "rolled back aicp.yaml"
  fi
  systemctl restart aicp 2>&1 | tail -3
  sleep 6
  echo "after rollback active=$(systemctl is-active aicp)"
  echo "=== deploy FAILED $(date -Is) ==="
  exit 1
fi
"""


_AUTH_SCRIPT = r"""#!/usr/bin/env bash
set -u
DST="$1"
MODE="$2"
SECRET_FILE=/root/.aicp_bio_secret
UNIT=/etc/systemd/system/aicp.service
SECRET="${3:-}"

cd "$DST" || exit 2

if [ -n "$SECRET" ]; then
  printf %s "$SECRET" > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
  echo "secret written to $SECRET_FILE"
fi

python3 - "$DST" "$MODE" <<'PY'
import re, sys, pathlib
d, mode = sys.argv[1], sys.argv[2]
p = pathlib.Path(d) / 'aicp.yaml'
t = p.read_text(encoding='utf-8')
lines = t.splitlines(keepends=True)
out = []
i = 0
n = len(lines)
while i < n:
    ln = lines[i]
    if re.match(r'^auth:\s*$', ln):
        out.append(ln)
        out.append('  mode: ' + mode + '\n')
        i += 1
        while i < n:
            cur = lines[i]
            if cur.strip() == '' or cur[:1] not in (' ', '\t'):
                break
            if not re.match(r'^\s*mode:\s', cur):
                out.append(cur)
            i += 1
        continue
    out.append(ln)
    i += 1
p.write_text(''.join(out), encoding='utf-8')
print('yaml auth.mode=' + mode)
PY

if [ -s "$SECRET_FILE" ]; then
  if grep -q '^Environment=BIO_SECRET=' "$UNIT" 2>/dev/null; then
    sed -i "s|^Environment=BIO_SECRET=.*|Environment=BIO_SECRET=$(cat "$SECRET_FILE")|" "$UNIT"
  else
    sed -i "/^\[Service\]/a Environment=BIO_SECRET=$(cat "$SECRET_FILE")" "$UNIT"
  fi
  systemctl daemon-reload
  echo "secret set"
else
  echo "no secret file, keep existing BIO_SECRET"
fi

systemctl restart aicp 2>&1 | tail -3
sleep 6
echo "active=$(systemctl is-active aicp)"
grep -A3 '^auth:' "$DST/aicp.yaml"
"""


def _wsl_exe() -> str:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = Path(system_root) / "System32" / "wsl.exe"
    if candidate.exists():
        return str(candidate)
    return shutil.which("wsl.exe") or "wsl.exe"


def _is_windows() -> bool:
    return os.name == "nt"


def _sh_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


async def _run_wsl(args, timeout: int = DEFAULT_TIMEOUT) -> dict:
    cmd = [_wsl_exe()] + list(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"rc": -1, "stdout": "", "stderr": "wsl.exe 不存在，未安装 WSL"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"rc": -1, "stdout": "", "stderr": f"命令超时（>{timeout}s）"}
    return {
        "rc": proc.returncode,
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
    }


async def _run_in(distro: str, command: str, timeout: int) -> dict:
    return await _run_wsl(
        ["-d", distro, "-e", "bash", "-lc", command],
        timeout=timeout,
    )


async def _wsl_ip(distro: str, timeout: int = 30) -> str:
    """取 WSL 实例在宿主可访问的 IPv4（过滤回环/链路本地，保留 WSL NAT 段）。"""
    cmd = (
        r"hostname -I 2>/dev/null | tr ' ' '\n' | "
        r"grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | "
        r"grep -v '^127\.' | grep -v '^169\.254\.' | head -1"
    )
    res = await _run_in(distro, cmd, timeout)
    for line in res["stdout"].splitlines():
        line = line.strip()
        if line:
            return line
    return ""


_KEEPALIVE_NAME = "aicp_keepalive.cmd"
_KEEPALIVE_CHECK_NAME = "aicp_keepalive_check.cmd"

_KEEPALIVE_TEMPLATE = """@echo off
REM AICP WSL keepalive -- keeps the distro alive and the service healthy.
REM WSL2 recycles the whole distro once the last Windows client exits, taking
REM systemd's {service} down with it (port {port} becomes flaky).
REM Anchor: one wsl.exe running 'sleep infinity' that never returns, so the
REM distro always has a live client.
REM Guardian: a loop that starts the service when it is not active.
REM   'systemctl start' is idempotent; 'systemctl restart' is not -- using
REM   restart here is what used to make the service flap.
REM AICP_ANCHOR=1 is a marker so the watchdog can spot the anchor.
start "" /min wsl.exe -d {distro} -e bash -lc "export AICP_ANCHOR=1; exec sleep infinity"
:loop
wsl.exe -d {distro} -e bash -lc "systemctl is-active --quiet {service} || systemctl start {service}"
ping -n 31 127.0.0.1 >nul
goto loop
"""

_KEEPALIVE_CHECK_TEMPLATE = """@echo off
REM AICP WSL watchdog -- relaunches the keepalive wrapper when the anchor dies.
REM The anchor is the never-exiting wsl.exe client ('sleep infinity') that keeps
REM the WSL distro from being recycled; without it systemd's aicp goes down with
REM the distro and port {port} becomes flaky.
REM Reload happens when EITHER the anchor or the wrapper is missing, so the
REM wrapper always respawns exactly one anchor (duplicates are killed first).
REM The pattern is split ('AICP_' + 'ANCHOR=1') so this script's own command
REM line never matches the filter.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$as = @(Get-CimInstance Win32_Process | Where-Object {{ $_.Name -eq 'wsl.exe' -and $_.CommandLine -like ('*AICP_' + 'ANCHOR=1*') }} | Sort-Object ProcessId); $ws = @(Get-CimInstance Win32_Process | Where-Object {{ $_.Name -eq 'cmd.exe' -and $_.CommandLine -like ('*aicp_keep' + 'alive.cmd*') }}); $need = ($as.Count -lt 1 -or $ws.Count -lt 1); if (-not $need -and $as.Count -gt 1) {{ $as | Select-Object -Skip 1 | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }} }}; if ($need) {{ $as | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}; $ws | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}; Start-Process -FilePath '{keepalive}' -WindowStyle Hidden }}"
"""


def _keepalive_path() -> str:
    return str(Path.home() / _KEEPALIVE_NAME)


def _keepalive_check_path() -> str:
    return str(Path.home() / _KEEPALIVE_CHECK_NAME)


def _write_keepalive(distro: str, service: str, port: int = 9200) -> str:
    path = _keepalive_path()
    text = _KEEPALIVE_TEMPLATE.format(distro=distro, service=service, port=port)
    Path(path).write_text(text.replace("\n", "\r\n"), encoding="utf-8")
    return path


def _write_keepalive_check(port: int = 9200) -> str:
    path = _keepalive_check_path()
    text = _KEEPALIVE_CHECK_TEMPLATE.format(port=port, keepalive=_keepalive_path())
    Path(path).write_text(text.replace("\n", "\r\n"), encoding="utf-8")
    return path


async def _spawn_detached(args) -> dict:
    """后台启动，不继承父进程管道（否则父进程读 stdout 会永久挂住）。"""
    import subprocess
    kwargs = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    except FileNotFoundError:
        return {"rc": -1, "started": False, "error": "命令不存在: " + str(args[0])}
    return {"rc": 0, "started": True, "pid": proc.pid}


def _port_alive(host: str, port: int, timeout: float = 2.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


async def _keepalive_clients() -> list:
    """列出当前存活的 wsl.exe 客户端进程。"""
    res = await _run_win(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'wsl.exe' } | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }",
        ],
        60,
    )
    rows = []
    for line in (res.get("stdout") or "").splitlines():
        line = line.strip()
        if line:
            rows.append(line)
    return rows


async def _kill_keepalive_clients() -> int:
    rows = await _keepalive_clients()
    killed = 0
    for row in rows:
        # 锚点标记是 AICP_ANCHOR=1；更早的脚本用 AICP_KEEPALIVE=1 或 "exec sleep infinity"。
        # 三种都要清掉，否则残留的常驻客户端会让标记检测失真。
        if not any(m in row for m in ("AICP_ANCHOR=1", "AICP_KEEPALIVE=1", "exec sleep infinity")):
            continue
        pid = row.split("|", 1)[0].strip()
        if not pid.isdigit():
            continue
        await _run_win(["taskkill", "/F", "/PID", pid], 20)
        killed += 1
    return killed


async def _ensure_keepalive() -> dict:
    """幂等拉起 keepalive：已有常驻客户端时不再重复启动。"""
    rows = await _keepalive_clients()
    if any("AICP_ANCHOR=1" in r for r in rows):
        return {"started": False, "already": True, "clients": rows}
    res = await _spawn_detached(["cmd", "/c", "start", "", '"' + _keepalive_path() + '"'])
    return {"started": bool(res.get("started")), "already": False, "clients": rows, "pid": res.get("pid")}


async def _run_win(args, timeout: int = 30) -> dict:
    """在 Windows 宿主直接执行命令（不带 shell，避免转义地狱）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"rc": -1, "stdout": "", "stderr": f"命令不存在: {args[0]}"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"rc": -1, "stdout": "", "stderr": f"命令超时（>{timeout}s）"}
    return {
        "rc": proc.returncode,
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
    }


def _payload_of(envelop) -> dict:
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    inner = raw.get("payload")
    return inner if isinstance(inner, dict) else raw


async def execute(envelop, agent):
    try:
        payload = _payload_of(envelop)
        action = str(payload.get("action") or "run")

        if not _is_windows():
            envelop.payload = {"ok": False, "error": "WSL 通道仅在 Windows 宿主可用"}
            return envelop

        if action == "run":
            command = str(payload.get("command") or "").strip()
            if not command:
                envelop.payload = {"ok": False, "error": "command 不能为空"}
                return envelop
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            timeout = int(payload.get("timeout_s") or DEFAULT_TIMEOUT)
            timeout = max(5, min(timeout, MAX_TIMEOUT))
            result = await _run_in(distro, command, timeout)
            envelop.payload = {
                "ok": result["rc"] == 0,
                "data": {
                    "distro": distro,
                    "rc": result["rc"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"],
                },
            }
            return envelop

        if action == "distros":
            result = await _run_wsl(["--list", "--verbose"], timeout=30)
            text = result["stdout"]
            if not text.strip():
                text = result["stderr"]
            try:
                text = text.encode("utf-8", "surrogateescape").decode("utf-16-le", "replace")
            except Exception:
                pass
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            envelop.payload = {"ok": True, "data": {"rc": result["rc"], "distros": lines}}
            return envelop

        if action == "read_file":
            path = str(payload.get("path") or "").strip()
            if not path:
                envelop.payload = {"ok": False, "error": "path 不能为空"}
                return envelop
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            timeout = int(payload.get("timeout_s") or DEFAULT_TIMEOUT)
            timeout = max(5, min(timeout, MAX_TIMEOUT))
            result = await _run_in(distro, f"cat -- {_sh_quote(path)}", timeout)
            envelop.payload = {
                "ok": result["rc"] == 0,
                "data": {
                    "path": path,
                    "rc": result["rc"],
                    "content": result["stdout"],
                    "stderr": result["stderr"],
                },
            }
            return envelop

        if action == "write_file":
            path = str(payload.get("path") or "").strip()
            content = payload.get("content")
            if not path:
                envelop.payload = {"ok": False, "error": "path 不能为空"}
                return envelop
            if content is None:
                envelop.payload = {"ok": False, "error": "content 不能为空"}
                return envelop
            if not isinstance(content, str):
                content = str(content)
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            timeout = int(payload.get("timeout_s") or DEFAULT_TIMEOUT)
            timeout = max(5, min(timeout, MAX_TIMEOUT))
            b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
            command = (
                f"mkdir -p -- $(dirname -- {_sh_quote(path)}) && "
                f"printf %s {_sh_quote(b64)} | base64 -d > {_sh_quote(path)}"
            )
            result = await _run_in(distro, command, timeout)
            envelop.payload = {
                "ok": result["rc"] == 0,
                "data": {
                    "path": path,
                    "rc": result["rc"],
                    "bytes": len(content.encode("utf-8")),
                    "stderr": result["stderr"],
                },
            }
            return envelop

        if action == "env":
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            timeout = int(payload.get("timeout_s") or 60)
            timeout = max(5, min(timeout, MAX_TIMEOUT))
            script = (
                "echo '--- os ---'; cat /etc/os-release 2>/dev/null | head -3; "
                "echo '--- kernel ---'; uname -a; "
                "echo '--- cpu ---'; nproc; "
                "echo '--- mem ---'; free -h 2>/dev/null | head -2; "
                "echo '--- disk ---'; df -h / | tail -1; "
                "echo '--- python ---'; python3 -V 2>&1; "
                "echo '--- ip ---'; hostname -I 2>/dev/null"
            )
            result = await _run_in(distro, script, timeout)
            envelop.payload = {
                "ok": result["rc"] == 0,
                "data": {"distro": distro, "rc": result["rc"], "info": result["stdout"]},
            }
            return envelop

        if action == "wsl_ip":
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            timeout = int(payload.get("timeout_s") or 30)
            timeout = max(5, min(timeout, MAX_TIMEOUT))
            ip = await _wsl_ip(distro, timeout)
            port = int(payload.get("port") or 9200)
            envelop.payload = {
                "ok": bool(ip),
                "data": {
                    "distro": distro,
                    "ip": ip,
                    "port": port,
                    "url": ("http://" + ip + ":" + str(port)) if ip else "",
                    "local_url": "http://127.0.0.1:" + str(port),
                },
            }
            return envelop

        if action == "autostart":
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            task_name = str(payload.get("task_name") or "AICP-WSL")
            enable = payload.get("enable", True)
            service = str(payload.get("service") or "aicp")
            port = int(payload.get("port") or 9200)
            # 1) WSL 内 systemd 服务开机启用
            ens = await _run_in(distro, "systemctl enable " + service + " 2>&1", 60)
            st = await _run_in(distro, "systemctl start " + service + " 2>&1", 90)
            svc = await _run_in(distro, "systemctl is-active " + service + " 2>&1", 30)
            # 2) Windows 登录自启：注册「常驻 keepalive」脚本。
            #    这里原来写的是短命命令 `wsl.exe -d ... systemctl start aicp`——命令退出后
            #    WSL2 回收整个发行版，systemd 里的 aicp.service 随之消失 → 9200 断连。
            ka_path = _write_keepalive(distro, service, port)
            _write_keepalive_check(port)
            tr = '"{ka}"'.format(ka=ka_path)
            reg_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
            wd_task = task_name + "_watchdog"
            wd_res = {"rc": -1, "stdout": "", "stderr": "未启用"}
            if enable:
                res = await _run_win(
                    ["reg.exe", "ADD", reg_key, "/v", task_name, "/t", "REG_SZ", "/d", tr, "/f"], 30
                )
                if res["rc"] != 0:
                    args = ["/Create", "/TN", task_name, "/TR", tr, "/SC", "ONLOGON", "/F"]
                    res = await _run_win(["schtasks.exe"] + args, 60)
                    qres = await _run_win(["schtasks.exe", "/Query", "/TN", task_name], 30)
                else:
                    qres = await _run_win(
                        ["reg.exe", "QUERY", reg_key, "/v", task_name], 30
                    )
                # 2.1) 看门狗：每分钟探测端口；端口不通且无常驻客户端 → 重新拉起 keepalive
                wd_res = await _run_win(
                    ["schtasks.exe", "/Create", "/TN", wd_task, "/TR",
                     '"' + _keepalive_check_path() + '"', "/SC", "MINUTE", "/MO", "1", "/F"], 60
                )
            else:
                res = await _run_win(["reg.exe", "DELETE", reg_key, "/v", task_name, "/f"], 30)
                qres = res
                wd_res = await _run_win(["schtasks.exe", "/Delete", "/TN", wd_task, "/F"], 30)
                await _kill_keepalive_clients()
            ip = await _wsl_ip(distro, 30)
            port = int(payload.get("port") or 9200)
            ka_clients = await _keepalive_clients()
            envelop.payload = {
                "ok": res["rc"] == 0,
                "data": {
                    "enabled": enable,
                    "task_name": task_name,
                    "rc": res["rc"],
                    "stdout": res["stdout"],
                    "stderr": res["stderr"],
                    "systemd_enable": ens["stdout"].strip(),
                    "systemd_start": st["stdout"].strip(),
                    "service_active": svc["stdout"].strip(),
                    "task_query": qres["stdout"].strip(),
                    "watchdog_task": wd_task,
                    "watchdog_rc": wd_res["rc"],
                    "keepalive_script": ka_path,
                    "keepalive_check_script": _keepalive_check_path(),
                    "keepalive_clients": ka_clients,
                    "keepalive_active": any("AICP_ANCHOR=1" in r for r in ka_clients),
                    "ip": ip,
                    "port": port,
                    "url": ("http://" + ip + ":" + str(port)) if ip else "",
                    "local_url": "http://127.0.0.1:" + str(port),
                },
            }
            return envelop

        if action == "keepalive":
            # 手动管理常驻 wsl.exe 客户端：保证 WSL2 不回收发行版，9200 不断连。
            sub = str(payload.get("sub") or "status").strip().lower()
            port = int(payload.get("port") or 9200)
            if sub == "status":
                rows = await _keepalive_clients()
                ip = await _wsl_ip(str(payload.get("distro") or DEFAULT_DISTRO), 30)
                alive = [r for r in rows if "AICP_ANCHOR=1" in r]
                envelop.payload = {
                    "ok": True,
                    "data": {
                        "port": port,
                        "port_alive": _port_alive("127.0.0.1", port),
                        "keepalive_active": bool(alive),
                        "keepalive_clients": alive,
                        "all_wsl_clients": rows,
                        "ip": ip,
                        "url": ("http://" + ip + ":" + str(port)) if ip else "",
                    },
                }
                return envelop
            if sub == "start":
                res = await _ensure_keepalive()
                await asyncio.sleep(2)
                envelop.payload = {
                    "ok": True,
                    "data": {
                        "started": res.get("started"),
                        "already": res.get("already"),
                        "pid": res.get("pid"),
                        "port_alive": _port_alive("127.0.0.1", port),
                        "keepalive_script": _keepalive_path(),
                    },
                }
                return envelop
            if sub == "stop":
                killed = await _kill_keepalive_clients()
                envelop.payload = {"ok": True, "data": {"killed": killed}}
                return envelop
            envelop.payload = {"ok": False, "error": "未知 sub: " + sub + "（可用: status/start/stop）"}
            return envelop

        if action == "issue_token":
            user_id = str(payload.get("user_id") or "local").strip()
            if not user_id:
                envelop.payload = {"ok": False, "error": "user_id 不能为空"}
                return envelop
            ttl = int(payload.get("ttl_seconds") or 30 * 86400)
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            secret = str(payload.get("secret") or "").strip()
            if not secret:
                sres = await _run_in(distro, "cat /root/.aicp_bio_secret 2>/dev/null", 30)
                secret = sres["stdout"].strip()
            source = "param" if str(payload.get("secret") or "").strip() else "wsl_secret_file"
            if not secret:
                secret = "change-me-in-prod"
                source = "default_fallback"
            exp = int(time.time()) + ttl
            body = {"uid": user_id, "exp": exp}
            raw = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True).encode()).decode().rstrip("=")
            sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()
            sig_b = base64.urlsafe_b64encode(sig).decode().rstrip("=")
            token = raw + "." + sig_b
            envelop.payload = {
                "ok": True,
                "data": {
                    "token": token,
                    "user_id": user_id,
                    "exp": exp,
                    "expires_in": ttl,
                    "secret_source": source,
                    "usage": "Authorization: Bearer <token> 或 X-AICP-Token: <token>",
                },
            }
            return envelop

        if action == "configure_auth":
            mode = str(payload.get("mode") or "hybrid").strip()
            if mode not in ("local", "hybrid", "token"):
                envelop.payload = {"ok": False, "error": "mode 必须是 local / hybrid / token"}
                return envelop
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            target_dir = str(payload.get("target_dir") or "/root/aicp").strip()
            secret = str(payload.get("secret") or "").strip()
            script = _AUTH_SCRIPT
            args = [_sh_quote(target_dir), _sh_quote(mode)]
            if secret:
                args.append(_sh_quote(secret))
            cmd = "bash -c " + _sh_quote(script) + " -- " + " ".join(args)
            res = await _run_in(distro, cmd, 180)
            ip = await _wsl_ip(distro, 30)
            port = int(payload.get("port") or 9200)
            envelop.payload = {
                "ok": res["rc"] == 0,
                "data": {
                    "distro": distro,
                    "mode": mode,
                    "target_dir": target_dir,
                    "secret_set": bool(secret),
                    "ip": ip,
                    "port": port,
                    "url": ("http://" + ip + ":" + str(port)) if ip else "",
                    "local_url": "http://127.0.0.1:" + str(port),
                    "rc": res["rc"],
                    "stdout": res["stdout"],
                    "stderr": res["stderr"],
                },
            }
            return envelop

        if action == "deploy":
            source_dir = str(payload.get("source_dir") or os.getcwd()).strip()
            target_dir = str(payload.get("target_dir") or "/root/aicp").strip()
            port = int(payload.get("port") or 9200)
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            script_path = "/root/aicp_deploy.sh"
            log_path = "/root/aicp_deploy.log"

            def _to_wsl_path(p: str) -> str:
                p = str(p).strip().replace("\\", "/")
                if not p:
                    return ""
                if len(p) > 1 and p[1] == ":":
                    drive = p[0].lower()
                    rest = p[2:].lstrip("/")
                    return "/mnt/" + drive + ("/" + rest if rest else "")
                if p.startswith("/"):
                    return p
                return ""

            src = _to_wsl_path(source_dir)
            if not src:
                envelop.payload = {"ok": False, "error": "source_dir 无法映射到 WSL 的 /mnt 路径: " + source_dir}
                return envelop

            default_excludes = [
                "./.git",
                "./_backup",
                "./data/memories",
                "./data/workspace",
                "./data/cache",
                "./data/uploads",
                "./data/registry_cache.json",
            ]
            raw_excludes = payload.get("exclude")
            if raw_excludes is None:
                excl = default_excludes
            elif isinstance(raw_excludes, str):
                excl = [x.strip() for x in raw_excludes.split(",") if x.strip()]
            elif isinstance(raw_excludes, (list, tuple)):
                excl = [str(x).strip() for x in raw_excludes if str(x).strip()]
            else:
                excl = default_excludes
            excl = [x for x in excl if re.match(r"^[\w./\-*]+$", x)]
            excl_args = " ".join("--exclude=" + _sh_quote(x) for x in excl)
            script = (
                _DEPLOY_SCRIPT.replace("__SRC__", src)
                .replace("__DST__", target_dir)
                .replace("__PORT__", str(port))
                .replace("__EXCLUDES__", excl_args)
            )
            b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
            write_cmd = (
                "printf %s " + _sh_quote(b64) + " | base64 -d > " + _sh_quote(script_path)
                + " && chmod +x " + _sh_quote(script_path)
            )
            wr = await _run_in(distro, write_cmd, 60)
            if wr["rc"] != 0:
                envelop.payload = {"ok": False, "error": "写入部署脚本失败", "data": {"stderr": wr["stderr"]}}
                return envelop

            launch = (
                "setsid --fork nohup bash " + _sh_quote(script_path)
                + " > " + _sh_quote(log_path) + " 2>&1 < /dev/null; sleep 1; pgrep -f aicp_deploy.sh | head -3"
            )
            lr = await _run_in(distro, launch, 30)
            envelop.payload = {
                "ok": True,
                "data": {
                    "distro": distro,
                    "source_dir": source_dir,
                    "source_wsl_path": src,
                    "target_dir": target_dir,
                    "port": port,
                    "script": script_path,
                    "log": log_path,
                    "pids": lr["stdout"].strip(),
                    "stderr": lr["stderr"],
                    "hint": "用 deploy_status 轮询日志与 systemd 状态",
                },
            }
            return envelop

        if action == "deploy_status":
            distro = str(payload.get("distro") or DEFAULT_DISTRO)
            timeout = int(payload.get("timeout_s") or 60)
            timeout = max(5, min(timeout, MAX_TIMEOUT))
            cmd = (
                "echo '--- deploy log ---'; tail -n 60 /root/aicp_deploy.log 2>/dev/null; "
                "echo '--- systemctl ---'; systemctl is-active aicp 2>&1; "
                "systemctl status aicp --no-pager -n 8 2>&1 | tail -n 14; "
                "echo '--- port ---'; ss -ltnp 2>/dev/null | grep -E ':(9200|9201|9202)' || true"
            )
            result = await _run_in(distro, cmd, timeout)
            envelop.payload = {
                "ok": True,
                "data": {"distro": distro, "rc": result["rc"], "info": result["stdout"], "stderr": result["stderr"]},
            }
            return envelop

        envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
        return envelop

    except Exception as e:
        try:
            agent.log.error(f"wsl plugin error: {e}")
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop
