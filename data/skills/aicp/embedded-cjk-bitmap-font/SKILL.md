---
title: 构建嵌入式紧凑中文位图字库
description: 用 1bpp 位图加 deflate 压缩加独立度量表构建 ESP32 级中文字库（用户说「中文显示不出来」「缺字」「how to render CJK on embedded」时用），比 LVGL 8bpp 静态字库省 39 倍
category: aicp
tags: [embedded, font, lvgl, esp32, cjk]
triggers: 中文显示, 缺字, 字库, 字体太大, flash不够, 位图字库, 栅格化, 压缩字库, 中文字体, 码位, 抗锯齿, CJK, bitmap, font, deflate, glyph, TTF, flash size, antialiasing, 1bpp
not_triggers: 网页字体, webfont, CSS字体, 打印, 排版设计, 海报, 书法
examples: "中文显示不出来，屏幕上一堆方块", "字库太大塞不进 flash 了", "how to render CJK on esp32 with limited flash"
version: 1.0
updated_at: 2026-11-11
---

# 构建嵌入式紧凑中文位图字库

## 何时用

- ESP32 / LVGL 设备上中文字数不够用，加字就编译不通过（用户说「中文显示不出来」「缺字」时）
- flash 分区装不下 LVGL 静态字库，需要压缩（用户说「字库太大塞不进 flash 了」时）
- 要让字库可增量扩展、想改存储格式（用户说「中文显示不出来，屏幕上一堆方块」时）
- 讨论 TTF / FreeType / TinyTTF 在 MCU 上的可行性（用户说「how to render CJK on esp32 with limited flash」时）

**不适用**：网页字体，webfont，CSS字体，打印，排版设计，海报，书法。

## 前置条件

- 本机 Python 3.11，依赖 `Pillow`（`ImageFont` + `ImageDraw`）或 `freetype-py`；`deflate` 用标准库 `zlib`
- 字体源文件：一份中文 TTF/OTF（如思源黑体、Noto Sans CJK）
- 目标固件侧能放二进制 blob（C 数组或 `.bin` 段）
- 若走 inflate 方案，需要固件侧有 zlib（ESP-IDF 用 `espressif/esp-zlib` 组件）
- 需要 LLM 时先兜底：`if not agent.llm: return {"data": {"error": "LLM 未配置"}}`

## 核心步骤

**1. 选定目标字符集并统计**

一级常用字 3500 字够用；若要更大覆盖（如 21,207 字）需要评估 flash 余量。先确定字表来源与字数，写入 `charset.txt`（一行一个 Unicode 码位）。

**2. 离线生成 1bpp 位图**

用 PIL 以 16px 渲染每个字，阈值化到 1 bit（`mode="1"`），得到 16×16=32 字节的字形。关键：逐字记录 advance（字宽）与 bearing，用于度量表。

```python
from PIL import Image, ImageDraw, ImageFont
font = ImageFont.truetype("SourceHanSansSC.ttf", 16)
img = Image.new("1", (16, 16), 0)
ImageDraw.Draw(img).text((0, 0), ch, fill=1, font=font)
bits = img.tobytes()  # 1bpp, 16x16 => 32 bytes
```

**3. 打包成 blob + deflate**

所有字形按 Unicode 排序顺序连续拼接，`zlib.compress(blob, 9)`。同时输出未压缩版本以便核对。压缩比通常 60-75%（1bpp 点阵上 deflate 效果差于文本，需实测）。

**4. 生成度量表（单独一张表）**

度量（advance/bearing）与字形数据**分离存储**，各自独立压缩。理由：度量表小且可裁剪，字形 blob 大；分离后可只升级字形不动度量。

**5. 生成 Unicode → 字形序号索引**

排序后的码位数组用「连续段 + 二分」或稀疏索引表编码，把查表成本压到 O(log n)。这一层通常只占几 KB。

**6. 固件侧实现 blit**

查索引拿序号 → 查度量拿 advance → 从 blob 取 32 字节位图 → 展开成 RGB565 写入画布。放弃 LVGL `lv_label` 的中文显示，用自绘的 draw-text 函数。

**7. 可选：高频字升级为 2bpp/更大字号**

挑 200 个最高频字额外存一份 2bpp 24px 版本（约 29KB），运行时按字符优先级选择。低成本换高频字观感。

**8. 可选：SDF 程序化生成符号**

`° ℃ · – — … −` 这类符号用有符号距离场在运行时画出来，不占任何存储。

```c
// 抗锯齿 SDF 采样，coverage 即 alpha
float cov = fminf(1, fmaxf(0, .5f - distance * scale));
```

## 判断规则

| 条件 | 动作 |
|---|---|
| flash 余量 > 字库体积 × 3 | 直接存未压缩 blob，省去 inflate 复杂度 |
| 余量紧张 | deflate 压缩 + 固件侧 inflate 到 heap buffer |
| 余量极紧（< 50KB） | 1bpp + 子集化到 1000-2000 字 + 高频字单独升级 |
| 需要任意字（真机、离线） | 走 TinyTTF + 运行时栅格化，配外部 flash 存 TTF |
| 需要真机且 flash ≥ 8MB | FreeType + esp_lvgl_port，渲染质量最好 |
| 目标只是模拟器演示 | 走本 skill 的位图字库路线，不折腾 TTF |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 用 LVGL 8bpp 静态字库 | 303 字就占 260KB，每字 858 字节 | `lv_font_fmt_txt_glyph_dsc_t` 是 8bpp 灰度，16×16 = 256 字节 + 描述符 | 改 1bpp，每字原始 32 字节 |
| 直接塞中文 TTF | flash 装不下 | Noto CJK 全字库 10MB+ | 子集化或改位图字库 |
| 以为字库必然爆体积 | 误判方案不可行 | 21,207 字 1bpp deflate 后仅 463KB，每字 21.9 字节 | 用实测压缩比重算 |
| 度量与字形混在一起 | 想裁剪字库时连带丢度量 | 单一大结构 | 度量表独立文件独立压缩 |
| TinyTTF 想走全量 TTF | 真机上还是死路 | TTF 体积远超 MCU flash | 模拟器可挂 host 文件，真机不行 |
| 缺字就重编译固件 | 迭代极慢 | 字库编译期烘进固件 | 字库做成独立 blob 段，只重烧数据段 |
| 1bpp 小字号锯齿明显 | 16px 中文发毛 | 1bpp 无灰度 | 高频字升级 2bpp，或换更大字号 |
| deflate 压缩比想当然 | 实测与理论差很多 | 点阵数据熵高 | 必测：算原始体积 × 实测压缩比 |

## 验证

用 code_runner 实测体积与查表正确性，骨架必须包含 `async def execute(envelop, agent):`，所有逻辑（含 import）写在函数内，返回 `{"data": ...}`。沙箱禁令：禁 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec。

```python
async def execute(envelop, agent):
    import zlib, json
    from PIL import Image, ImageDraw, ImageFont

    chars = "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感"
    font = ImageFont.truetype("SourceHanSansSC.ttf", 16)
    blob = bytearray()
    advances = []
    for ch in chars:
        img = Image.new("1", (16, 16), 0)
        ImageDraw.Draw(img).text((0, 0), ch, fill=1, font=font)
        blob += img.tobytes()
        advances.append(font.getlength(ch))

    comp = zlib.compress(bytes(blob), 9)
    metrics = zlib.compress(json.dumps(advances).encode(), 9)

    raw, clen, mlen = len(blob), len(comp), len(metrics)
    per_glyph = clen / len(chars)
    return {"data": {
        "ok": clen < raw and mlen > 0,
        "glyphs": len(chars),
        "raw_bytes": raw,
        "deflate_bytes": clen,
        "metrics_bytes": mlen,
        "ratio": round(raw / clen, 3),
        "bytes_per_glyph": round(per_glyph, 1),
        "lvgl_8bpp_estimate": len(chars) * 858,
        "savings_vs_lvgl": round(len(chars) * 858 / max(clen, 1), 1),
    }}
```

断言清单：
- `ratio > 1.2`（deflate 确实有效）
- `bytes_per_glyph < 60`（达到 1bpp 位图路线的量级）
- `savings_vs_lvgl > 10`（相对 LVGL 8bpp 有数量级优势）
- 抽 10 个字，渲染结果与 `ImageFont` 原图逐像素比对，不一致则说明位序/行对齐错了

## 参考

- 相关 skill：`skill/aicp/aicp-sml-builder`
- 参考项目：`ai-maker-lab/HachimoDock`（固件字体走 1bpp + deflate + 独立 metrics + 列编码，21,207 字压到 463KB）；`liangdabiao/esp-mosaico-HachimoDock`（ESP32-S3 + 480×480 移植版）
- LVGL Kconfig 事实：`LV_USE_TINY_TTF` 存在于 LVGL 8.3.11，纯 C 内嵌于 lvgl 组件，无需 FreeType
- 相关插件：`builtins/tools/code_runner`、`builtins/tools/file_utils_api`
