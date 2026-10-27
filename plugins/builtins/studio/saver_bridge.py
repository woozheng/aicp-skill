"""Studio 代码保存桥接 — 调 saver API"""
import aiohttp


async def execute(envelop, agent):
    text = envelop.payload.get("text", "")
    if not text:
        envelop.payload = {"error": "No text"}
        return envelop

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{agent.base_url}/api/saver/save",
            json={"text": text},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            result = await resp.json()

    envelop.payload = result
    return envelop
