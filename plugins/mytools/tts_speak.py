"""
本机语音播报 —— 双引擎 TTS

引擎：
  edge    微软 Edge 神经网络语音（联网，音质最好，8 个中文音色，含方言）
  offline 本机 WinRT/OneCore 离线合成（瑶瑶/康康/慧慧，0.2 秒出音，断网可用）

engine: edge(默认) / offline / auto(先 edge，失败自动回落 offline)

action:
  speak  念一段话（默认等播放完）
         text(必填) engine? voice? rate? volume? pitch? wait? cache?
  save   只合成不播放，返回音频路径
         text(必填) engine? voice? rate? volume? pitch? out? cache?
  batch  多段文本依次合成（texts 列表），可只存不播
         texts(必填) play? engine? voice? rate?
  list   列出两个引擎的全部音色 + 缓存统计
  cache  只看缓存统计
  clear  清空音频缓存

音色简写：
  edge    xiaoxiao(默认女) xiaoyi yunxi(男) yunjian yunxia yunyang xiaobei(东北) xiaoni(陕西)
  offline yaoyao(默认女) kangkang(男) huihui
rate/volume/pitch 形如 "-5%" "+10%"
缓存：按 引擎+音色+语速+音量+音调+文本 哈希存盘，重复念同一句 0 毫秒
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid

EDGE_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "yunxia": "zh-CN-YunxiaNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "xiaobei": "zh-CN-liaoning-XiaobeiNeural",
    "xiaoni": "zh-CN-shaanxi-XiaoniNeural",
}
OFFLINE_VOICES = {
    "yaoyao": "Yaoyao",
    "kangkang": "Kangkang",
    "huihui": "Huihui",
}
DEFAULT_EDGE = "zh-CN-XiaoxiaoNeural"
DEFAULT_OFFLINE = "yaoyao"
CACHE_DIR = os.path.join("data", "tts_cache")
WORK_DIR = os.path.join("data", "workspace", "tts_offline")

OFFLINE_PS = r'''
$ErrorActionPreference = 'Stop'
$cfg = (Get-Content -Raw -Encoding UTF8 '__JOB__') | ConvertFrom-Json
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
  $m = $asTaskGeneric.MakeGenericMethod($type)
  $t = $m.Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}
[Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media,ContentType=WindowsRuntime] | Out-Null
$synth = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::new()
$all = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices
$v = $all | Where-Object { $_.Id -like ('*' + $cfg.voice + '*') } | Select-Object -First 1
if ($v) { $synth.Voice = $v }
try { $synth.Options.SpeakingRate = [double]$cfg.rate } catch { }
$stream = Await ($synth.SynthesizeTextToStreamAsync($cfg.text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
$net = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream)
$fs = [System.IO.File]::Create($cfg.out)
$net.CopyTo($fs)
$fs.Close()
$net.Close()
Write-Output 'SYNTH_OK'
'''


def _payload(envelop):
    raw = getattr(envelop, "payload", None)
    if isinstance(raw, dict):
        inner = raw.get("payload")
        if isinstance(inner, dict):
            return inner
        return raw
    return {}


def _resolve_edge_voice(v):
    if not v:
        return DEFAULT_EDGE
    s = str(v).strip()
    if s.lower() in EDGE_VOICES:
        return EDGE_VOICES[s.lower()]
    if "-" in s and "Neural" in s:
        return s
    return DEFAULT_EDGE


def _resolve_offline_voice(v):
    if not v:
        return OFFLINE_VOICES[DEFAULT_OFFLINE]
    s = str(v).strip().lower()
    if s in OFFLINE_VOICES:
        return OFFLINE_VOICES[s]
    for k, real in OFFLINE_VOICES.items():
        if k in s or real.lower() in s:
            return real
    return OFFLINE_VOICES[DEFAULT_OFFLINE]


def _fmt_pct(v, default="+0%"):
    if v is None or v == "":
        return default
    s = str(v).strip()
    if s.endswith("%"):
        return s if (s.startswith("-") or s.startswith("+")) else "+" + s
    if s.startswith("-") or s.startswith("+"):
        return s + "%"
    return "+" + s + "%"


def _fmt_hz(v, default="+0Hz"):
    """edge-tts 的 pitch 必须是 +NHz / -NHz，不接受百分号"""
    if v is None or v == "":
        return default
    s = str(v).strip()
    if s.lower().endswith("hz"):
        return s if (s.startswith("-") or s.startswith("+")) else "+" + s
    s2 = s.replace("%", "")
    try:
        n = int(float(s2) * 5)
    except Exception:
        n = 0
    return ("%+dHz" % n)


def _rate_num(pct):
    try:
        return max(0.5, min(6.0, 1.0 + float(str(pct).replace("%", "")) / 100.0))
    except Exception:
        return 1.0


def _cache_path(engine, voice, rate, volume, pitch, text):
    key = "|".join([engine, voice, rate, volume, pitch, text])
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    ext = "mp3" if engine == "edge" else "wav"
    return os.path.abspath(os.path.join(CACHE_DIR, "%s_%s_%s.%s" % (engine, voice.replace(":", "").replace("\\", "")[-12:], h, ext)))


async def _run(args, timeout=60):
    try:
        p = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return {"launch_error": str(e)}
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return {"timeout": True}
    return {
        "rc": p.returncode,
        "out": out.decode("utf-8", "ignore").strip()[:600],
        "err": err.decode("utf-8", "ignore").strip()[:600],
    }


async def _synth_edge(text, voice, rate, volume, pitch, out_path):
    r = await _run(
        [
            sys.executable, "-m", "edge_tts",
            "--voice", voice,
            "--rate=" + rate,
            "--volume=" + volume,
            "--pitch=" + pitch,
            "--text", text,
            "--write-media", out_path,
        ],
        timeout=45,
    )
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return {"ok": False, "engine": "edge", "error": "edge 合成失败（可能断网）", "detail": r}
    return {"ok": True, "engine": "edge", "file": out_path, "size": os.path.getsize(out_path)}


async def _synth_offline(text, voice, rate, out_path):
    os.makedirs(WORK_DIR, exist_ok=True)
    job = os.path.abspath(os.path.join(WORK_DIR, "job_%s.json" % uuid.uuid4().hex[:8]))
    ps1 = os.path.abspath(os.path.join(WORK_DIR, "synth.ps1"))
    with open(job, "w", encoding="utf-8-sig") as f:
        json.dump({"text": text, "voice": voice, "rate": _rate_num(rate), "out": out_path}, f, ensure_ascii=False)
    with open(ps1, "w", encoding="utf-8-sig") as f:
        f.write(OFFLINE_PS.replace("__JOB__", job.replace("'", "''")))
    t0 = time.perf_counter()
    r = await _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1],
        timeout=45,
    )
    ms = int((time.perf_counter() - t0) * 1000)
    try:
        os.remove(job)
    except Exception:
        pass
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return {"ok": False, "engine": "offline", "error": "offline 合成失败", "detail": r}
    return {"ok": True, "engine": "offline", "file": out_path, "size": os.path.getsize(out_path), "synth_ms": ms}


async def _play(path):
    esc = path.replace("\\", "\\\\")
    ps = (
        "Add-Type -AssemblyName PresentationCore; "
        "$p = New-Object System.Windows.Media.MediaPlayer; "
        '$p.Open([uri]::new("' + esc + '")); '
        "$d = 0; for ($i=0; $i -lt 60; $i++) { Start-Sleep -Milliseconds 100; "
        "if ($p.NaturalDuration.HasTimeSpan) { $d = $p.NaturalDuration.TimeSpan.TotalMilliseconds; if ($d -gt 0) { break } } }; "
        "$p.Play(); "
        "if ($d -gt 0) { Start-Sleep -Milliseconds ([int]$d + 400) } else { Start-Sleep -Milliseconds 3000 }; "
        '$p.Close(); "PLAY_DONE $d"'
    )
    return await _run(["powershell", "-NoProfile", "-Sta", "-Command", ps], timeout=180)


def _cache_stat():
    files, total = 0, 0
    if os.path.isdir(CACHE_DIR):
        for n in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, n)
            if os.path.isfile(p):
                files += 1
                total += os.path.getsize(p)
    return {"files": files, "size_mb": round(total / 1048576.0, 2), "dir": os.path.abspath(CACHE_DIR)}


async def _synth_one(text, engine, voice, rate, volume, pitch, use_cache, out_path=None):
    """返回 {ok, engine, file, size, cached} 或 {ok: False, error}"""
    engine = (engine or "edge").lower()
    tried = []
    if engine in ("edge", "auto"):
        ev = _resolve_edge_voice(voice)
        cp = _cache_path("edge", ev, rate, volume, pitch, text)
        if use_cache and os.path.exists(cp) and os.path.getsize(cp) > 0:
            return {"ok": True, "engine": "edge", "voice": ev, "file": cp, "size": os.path.getsize(cp), "cached": True}
        os.makedirs(CACHE_DIR, exist_ok=True)
        res = await _synth_edge(text, ev, rate, volume, pitch, cp)
        if res.get("ok"):
            res["voice"] = ev
            res["cached"] = False
            if out_path:
                try:
                    import shutil
                    shutil.copyfile(cp, out_path)
                    res["file"] = os.path.abspath(out_path)
                except Exception as e:
                    res["copy_error"] = str(e)
            return res
        tried.append({"engine": "edge", "error": res.get("error"), "detail": res.get("detail")})
        if engine == "edge":
            return {"ok": False, "error": "edge 合成失败", "tried": tried}

    ov = _resolve_offline_voice(voice)
    cp = _cache_path("offline", ov, rate, "+0%", "+0%", text)
    if use_cache and os.path.exists(cp) and os.path.getsize(cp) > 0:
        return {"ok": True, "engine": "offline", "voice": ov, "file": cp, "size": os.path.getsize(cp), "cached": True, "fallback": bool(tried)}
    os.makedirs(CACHE_DIR, exist_ok=True)
    res = await _synth_offline(text, ov, rate, cp)
    if res.get("ok"):
        res["voice"] = ov
        res["cached"] = False
        res["fallback"] = bool(tried)
        if tried:
            res["tried"] = tried
        if out_path:
            try:
                import shutil
                shutil.copyfile(cp, out_path)
                res["file"] = os.path.abspath(out_path)
            except Exception as e:
                res["copy_error"] = str(e)
        return res
    tried.append({"engine": "offline", "error": res.get("error"), "detail": res.get("detail")})
    return {"ok": False, "error": "两个引擎都失败", "tried": tried}


async def execute(envelop, agent):
    try:
        payload = _payload(envelop)
        action = str(payload.get("action") or "speak").strip().lower()

        if action == "list":
            envelop.payload = {
                "ok": True,
                "data": {
                    "engines": {
                        "edge": {"desc": "Edge 神经网络（联网，音质最好）", "default": DEFAULT_EDGE, "voices": EDGE_VOICES},
                        "offline": {"desc": "本机 WinRT 离线合成（0.2 秒，断网可用）", "default": OFFLINE_VOICES[DEFAULT_OFFLINE], "voices": OFFLINE_VOICES},
                        "auto": {"desc": "先 edge，失败自动回落 offline"},
                    },
                    "default_engine": "edge",
                    "cache": _cache_stat(),
                },
            }
            return envelop

        if action == "cache":
            envelop.payload = {"ok": True, "data": _cache_stat()}
            return envelop

        if action == "clear":
            removed = 0
            if os.path.isdir(CACHE_DIR):
                for n in os.listdir(CACHE_DIR):
                    p = os.path.join(CACHE_DIR, n)
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                            removed += 1
                        except Exception:
                            pass
            envelop.payload = {"ok": True, "data": {"removed": removed, "cache": _cache_stat()}}
            return envelop

        engine = str(payload.get("engine") or "edge").strip().lower()
        if engine not in ("edge", "offline", "auto"):
            engine = "edge"
        rate = _fmt_pct(payload.get("rate"))
        volume = _fmt_pct(payload.get("volume"))
        pitch = _fmt_hz(payload.get("pitch"))
        use_cache = payload.get("cache", True) is not False

        if action in ("speak", "save"):
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            voice = payload.get("voice")
            out = payload.get("out") if action == "save" else None
            if out:
                out = os.path.abspath(str(out))
                os.makedirs(os.path.dirname(out), exist_ok=True)
            res = await _synth_one(text, engine, voice, rate, volume, pitch, use_cache, out)
            if not res.get("ok"):
                envelop.payload = res
                return envelop
            if action == "save":
                res["played"] = False
                envelop.payload = {"ok": True, "data": res}
                return envelop
            if payload.get("wait", True) is False:
                asyncio.get_event_loop().create_task(_play(res["file"]))
                res["played"] = "async"
                envelop.payload = {"ok": True, "data": res}
                return envelop
            res["play"] = await _play(res["file"])
            res["played"] = True
            envelop.payload = {"ok": True, "data": res}
            return envelop

        if action == "batch":
            texts = payload.get("texts")
            if isinstance(texts, str):
                texts = [t for t in texts.split("\n") if t.strip()]
            if not isinstance(texts, list) or not texts:
                envelop.payload = {"ok": False, "error": "texts 必须是非空数组"}
                return envelop
            do_play = payload.get("play", True) is not False
            results = []
            for t in texts[:20]:
                t = str(t).strip()
                if not t:
                    continue
                r = await _synth_one(t, engine, payload.get("voice"), rate, volume, pitch, use_cache)
                item = {"text": t[:40], "ok": bool(r.get("ok")), "engine": r.get("engine"), "file": r.get("file"), "cached": r.get("cached")}
                if r.get("ok") and do_play:
                    await _play(r["file"])
                    item["played"] = True
                if not r.get("ok"):
                    item["error"] = r.get("error")
                results.append(item)
            envelop.payload = {"ok": True, "data": {"count": len(results), "items": results, "cache": _cache_stat()}}
            return envelop

        envelop.payload = {"ok": False, "error": "未知 action，可用: speak / save / batch / list / cache / clear"}
        return envelop

    except Exception as e:
        try:
            agent.log.error(str(e))
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop
