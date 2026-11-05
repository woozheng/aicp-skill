# tts_speak —— 双引擎本机语音播报

route: `mytools/tts_speak`

## 两个引擎

| engine | 说明 | 固定开销 | 联网 | 音色 |
|---|---|---|---|---|
| `edge`（默认） | 微软 Edge 神经网络 TTS | 1.4~9s（网络波动） | 必须 | 8 个，含东北/陕西话 |
| `offline` | 本机 WinRT/OneCore 合成 | 0.2~3s（首次含 PowerShell 启动） | 不需要 | 瑶瑶/康康/慧慧 |
| `auto` | 先 edge，失败自动回落 offline | — | 优先 | 同上 |

## action

### speak —— 合成并播放
```json
{"action":"speak","text":"要念的话","engine":"auto","voice":"yaoyao","rate":"-10%","volume":"+0%","pitch":"+10%","wait":true,"cache":true}
```
- `text` 必填
- `wait=false` 异步播放，立刻返回（适合长文本）
- `cache=false` 跳过缓存强制重新合成

### save —— 只合成不播
```json
{"action":"save","text":"...","engine":"edge","out":"data/workspace/x.mp3"}
```
返回 `data.file` / `data.size` / `data.engine` / `data.voice` / `data.cached`。

### batch —— 多段依次处理
```json
{"action":"batch","texts":["第一段","第二段"],"play":true,"engine":"offline"}
```
`texts` 也可传换行分隔字符串，最多 20 条。

### list / cache / clear
- `list` 全部音色 + 缓存统计
- `cache` 只看缓存
- `clear` 清空缓存

## 音色简写

- edge：`xiaoxiao`(默认女) `xiaoyi` `yunxi`(男) `yunjian` `yunxia` `yunyang` `xiaobei`(东北) `xiaoni`(陕西)
- offline：`yaoyao`(默认女) `kangkang`(男) `huihui`
- 也可直接传完整 ID，如 `zh-CN-XiaoxiaoNeural`

## 参数格式

- `rate` / `volume`：百分比 `"-10%"` / `"+20%"`
- `pitch`：可直接传 `"+20%"`（内部换算成 Hz），或传 `"+10Hz"`
  - ⚠️ edge-tts 底层只接受 `^[+-]\d+Hz$`，插件已做转换，不要绕过插件直接调 edge_tts CLI

## 缓存

key = `engine|voice|rate|volume|pitch|text` 的 md5，落盘 `data/tts_cache/`。
重复念同一句 → 0~1ms 直接命中。`clear` 可清空。

## 已知限制

- `offline` 每次要起一个 PowerShell 进程，首次 ~1.6s，比纯推理（157ms）多；命中缓存则 0ms。
- `edge` 耗时随网络波动（实测 1.4s ~ 9s），断网时用 `engine="auto"` 或 `offline`。
- `offline` 只支持中文三个音色，不支持方言/多语言。
