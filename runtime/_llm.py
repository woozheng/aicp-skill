"""
LLM — Production-grade multi-provider LLM client.

Supports OpenAI-compatible APIs with automatic
failover, retry logic, streaming, and comprehensive error handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import httpx
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("aicp.llm")


def _log(msg: str) -> None:
    """Structured debug logging."""
    logger.debug(msg)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REQUEST_TIMEOUT: float = 60.0
DEFAULT_STREAM_TIMEOUT: float = 300.0
DEFAULT_CONNECT_TIMEOUT: float = 10.0
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_MAX_CONCURRENT: int = 10

HTTP_TIMEOUT_CONFIG = {
    "connect": DEFAULT_CONNECT_TIMEOUT,
    "read": 90.0,
    "write": 30.0,
    "pool": DEFAULT_CONNECT_TIMEOUT,
}

# ---------------------------------------------------------------------------
# Error marker
# ---------------------------------------------------------------------------

ERROR_PREFIXES = (
    "[LLM stream error:",
    "[LLM请求失败:",
    "[系统错误:",
    "[服务请求超时",
    "[模型返回空响应",
    "[LLM 达到最大重试次数]",
    "[LLM 未配置]",
    "[空响应]",
)


def is_llm_error_string(text) -> bool:
    """检测文本是否是 LLM 错误包装"""
    if not isinstance(text, str):
        return False
    if not text:
        return False
    return any(text.startswith(p) for p in ERROR_PREFIXES)


# ---------------------------------------------------------------------------
# ★★★ <think> 剥离工具 ★★★
# ---------------------------------------------------------------------------

def _strip_leading_think(text: str) -> str:
    """剥掉开头的 <think>...</think> 段。

    规则：
    - 必须严格以 <think> 开头，且找到 </think> 才剥
    - 找不到 </think> → 返回空字符串（残缺 think 段，视为无效输出）
    - 没有 <think> 开头 → 原样返回

    注意：<think> 内部可能包含 { } 等字符，
    判断结束必须以 </think> 闭合标签为准，不能靠 { 推断。
    """
    if not text:
        return text

    stripped = text.lstrip()
    if not stripped.startswith("<think>"):
        return text

    close_idx = stripped.find("</think>")
    if close_idx == -1:
        return ""

    return stripped[close_idx + len("</think>"):].lstrip()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    """Validated model configuration."""
    client_name: str
    max_tokens: int = 4096
    temperature: float = 0.7
    supports_streaming: bool = True
    high_quality: bool = False


@dataclass
class ProviderConfig:
    """Provider configuration container."""
    name: str
    type: str  # "openai"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    models: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base exception for LLM-related errors."""
    pass


class LLMNotConfiguredError(LLMError):
    """Raised when no LLM client is configured."""
    pass


class LLMTimeoutError(LLMError):
    """Raised when an LLM request times out."""
    pass


class LLMEmptyResponseError(LLMError):
    """Raised when LLM returns an empty response."""
    pass


# ---------------------------------------------------------------------------
# Safe fallback constants
# ---------------------------------------------------------------------------

FALLBACK_MESSAGES = {
    "not_configured": "[LLM 未配置]",
    "empty_response": "[模型返回空响应，请稍后重试]",
    "timeout": "[服务请求超时，请稍后重试]",
    "max_retries": "[LLM 达到最大重试次数]",
    "system_error": "[系统错误]",
    "empty": "[空响应]",
}


# ---------------------------------------------------------------------------
# Main LLM class
# ---------------------------------------------------------------------------


class LLM:
    """Production-grade multi-provider LLM client."""

    def __init__(self, config: Dict[str, Any]) -> None:
        models_cfg = config.get("models", {})

        self._roles: Dict[str, str] = models_cfg.get("roles", {})
        self.default_model: str = self._roles.get(
            "default",
            models_cfg.get("default", "gpt-3.5-turbo"),
        )

        self.providers: Dict[str, Any] = models_cfg.get("providers", {})

        self._clients: Dict[str, AsyncOpenAI] = {}
        self._model_to_client: Dict[str, str] = {}
        self._model_configs: Dict[str, ModelConfig] = {}

        self.max_retries: int = models_cfg.get("max_retries", DEFAULT_MAX_RETRIES)
        self.request_timeout: float = models_cfg.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
        self.stream_timeout: float = models_cfg.get("stream_timeout", DEFAULT_STREAM_TIMEOUT)
        self.connect_timeout: float = models_cfg.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)

        max_concurrent: int = models_cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
        self._semaphore = asyncio.Semaphore(max_concurrent)

        _log(f"📋 Config models keys: {list(models_cfg.keys())}")
        _log(f"⏱️ request_timeout: {self.request_timeout}s")

        self._init_clients()

    # ======================================================================
    # Provider initialization
    # ======================================================================

    def _init_clients(self) -> None:
        for name, cfg in self.providers.items():
            provider = ProviderConfig(
                name=name,
                type=cfg.get("type", "openai"),
                api_key=cfg.get("api_key", ""),
                base_url=cfg.get("base_url", "https://api.openai.com/v1"),
                models=cfg.get("models", []),
            )
            self._init_openai_provider(provider)

        self._validate_default_model()

        _log(f"🤖 Default: {self.default_model} | Models: {len(self._model_to_client)}")
        if self._roles:
            _log(f"📋 Roles: {self._roles}")

    def _init_openai_provider(self, provider: ProviderConfig) -> None:
        if not provider.api_key or provider.api_key.startswith("${"):
            _log(f"⚠️ Skip {provider.name}: API key not configured")
            return

        try:
            timeout = httpx.Timeout(
                timeout=120.0,
                connect=self.connect_timeout,
                read=HTTP_TIMEOUT_CONFIG["read"],
                write=HTTP_TIMEOUT_CONFIG["write"],
                pool=self.connect_timeout,
            )

            client = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=timeout,
                http_client=httpx.AsyncClient(
                    timeout=timeout,
                    limits=httpx.Limits(
                        max_keepalive_connections=5,
                        max_connections=10,
                    ),
                ),
                max_retries=0,
            )
            self._clients[provider.name] = client

            for model_cfg in provider.models:
                model_id = model_cfg.get("id")
                if not model_id:
                    continue

                self._model_configs[model_id] = ModelConfig(
                    client_name=provider.name,
                    max_tokens=model_cfg.get("max_tokens", 4096),
                    temperature=model_cfg.get("temperature", 0.7),
                    supports_streaming=model_cfg.get("supports_streaming", True),
                    high_quality=model_cfg.get("high_quality", False),
                )
                self._model_to_client[model_id] = provider.name

                if model_cfg.get("high_quality"):
                    self.high_quality_model = model_id

                _log(f"✅ {model_id} ({provider.name})")

        except Exception as exc:
            _log(f"❌ Failed to init {provider.name}: {exc}")

    def _validate_default_model(self) -> None:
        if self.default_model not in self._model_to_client:
            if self._model_to_client:
                self.default_model = next(iter(self._model_to_client))
                _log(f"⚠️ roles.default 模型不可用，改用: {self.default_model}")
            else:
                _log("❌ 没有任何可用模型！")

    # ======================================================================
    # Model resolution
    # ======================================================================

    def _resolve_model(self, role: Optional[str] = None) -> str:
        if not role:
            return self.default_model

        model = self._roles.get(role)
        if model and model in self._model_to_client:
            return model

        if model:
            _log(f"⚠️ roles.{role}={model} 不可用，降级到 default")

        return self.default_model

    def _get_client(self, model: Optional[str] = None) -> tuple[Optional[AsyncOpenAI], str]:
        resolved_model = model or self.default_model
        client_name = self._model_to_client.get(resolved_model)

        if client_name and client_name in self._clients:
            return self._clients[client_name], resolved_model

        if self._clients:
            name = next(iter(self._clients))
            return self._clients[name], resolved_model

        return None, resolved_model

    # ======================================================================
    # Retry logic
    # ======================================================================

    @staticmethod
    def _calculate_backoff(attempt: int, max_wait: int = 10) -> float:
        import random
        wait = min(2 ** attempt, max_wait)
        jitter = random.uniform(0, wait * 0.5)
        return wait + jitter

    # ======================================================================
    # Core chat implementation
    # ======================================================================

    async def _chat_impl(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        client, actual_model = self._get_client(model)

        if client is None:
            return FALLBACK_MESSAGES["not_configured"]

        config = self._model_configs.get(actual_model, ModelConfig(client_name="unknown"))
        max_tokens = kwargs.pop("max_tokens", config.max_tokens)
        temperature = kwargs.pop("temperature", config.temperature)

        _log(f"🔍 实际请求 URL: {client.base_url}/chat/completions")

        last_error: Optional[str] = None

        for attempt in range(self.max_retries):
            try:
                _log(f"🔄 API call {attempt + 1}/{self.max_retries} → {actual_model}")
                t0 = time.time()

                task = asyncio.create_task(
                    client.chat.completions.create(
                        model=actual_model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        **kwargs,
                    )
                )

                try:
                    response = await asyncio.wait_for(
                        task,
                        timeout=self.request_timeout,
                    )
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise

                elapsed = time.time() - t0

                if (
                    response
                    and response.choices
                    and response.choices[0].message.content
                ):
                    result = response.choices[0].message.content.strip()
                    if result:
                        # ★★★ 剥离开头的 <think> 段 ★★★
                        result = _strip_leading_think(result)
                        _log(f"✅ 成功 ({elapsed:.1f}s, {len(result)} chars)")
                        return result

                _log(f"⚠️ 空响应 ({elapsed:.1f}s)")
                last_error = "empty_response"

            except asyncio.TimeoutError:
                _log(f"⏱️ 超时 attempt {attempt + 1}")
                last_error = "timeout"
            except Exception as exc:
                error_msg = str(exc)[:200]
                _log(f"❌ 错误 attempt {attempt + 1}: {type(exc).__name__}: {error_msg[:100]}")
                last_error = error_msg

            if attempt < self.max_retries - 1:
                wait = self._calculate_backoff(attempt)
                _log(f"⏳ 等待 {wait:.1f}s 后重试...")
                await asyncio.sleep(wait)

        if last_error == "timeout":
            return FALLBACK_MESSAGES["timeout"]
        elif last_error == "empty_response":
            return FALLBACK_MESSAGES["empty_response"]
        else:
            return f"[LLM请求失败: {last_error[:200]}]"

    # ======================================================================
    # Public API
    # ======================================================================

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        if model:
            resolved_model = model
        elif role:
            resolved_model = self._resolve_model(role)
        else:
            resolved_model = self.default_model

        _log(f"💬 chat: {resolved_model}" + (f" (role={role})" if role else ""))

        try:
            async with self._semaphore:
                result = await self._chat_impl(messages, resolved_model, **kwargs)

                if result is None:
                    return FALLBACK_MESSAGES["system_error"]
                if not isinstance(result, str):
                    result = str(result)
                if not result.strip():
                    return FALLBACK_MESSAGES["empty"]

                return result

        except Exception as exc:
            _log(f"❌ chat 异常: {type(exc).__name__}: {str(exc)[:100]}")
            return f"[系统错误: {str(exc)[:100]}]"

    # ======================================================================
    # Streaming
    # ======================================================================

    async def _chat_stream_impl(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        client, actual_model = self._get_client(model)

        if client is None:
            yield FALLBACK_MESSAGES["not_configured"]
            return

        _log(f"🔍 实际请求 URL: {client.base_url}/chat/completions")

        config = self._model_configs.get(actual_model, ModelConfig(client_name="unknown"))

        if not config.supports_streaming:
            result = await self._chat_impl(messages, model, **kwargs)
            if result and not result.startswith("["):
                yield result
            else:
                yield FALLBACK_MESSAGES["empty"]
            return

        max_tokens = kwargs.pop("max_tokens", config.max_tokens)
        temperature = kwargs.pop("temperature", config.temperature)

        for attempt in range(self.max_retries):
            stream = None
            try:
                _log(f"📡 stream {attempt + 1}/{self.max_retries} → {actual_model}")

                stream = await client.chat.completions.create(
                    model=actual_model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=True,
                    **kwargs,
                )

                # ★★★ 流式剥离状态机 ★★★
                #
                # 状态：
                #   in_think = None  → 还没决定，正在缓冲判断开头
                #   in_think = True  → 已确认开头是 <think>，憋着等 </think>
                #   in_think = False → 开头不是 <think>，正常流式吐
                #
                # 规则：
                #   - 缓冲够 DECISION_LEN 字符后决定
                #   - 进入 think 模式后，只认 </think>，不看 {
                #   - 流结束还没见到 </think> → 视作失败，吐 [空响应]
                #
                buffer = ""
                in_think = None
                DECISION_LEN = 10

                async for chunk in stream:
                    if not (chunk.choices and chunk.choices[0].delta.content):
                        continue
                    token = chunk.choices[0].delta.content
                    if not token:
                        continue

                    buffer += token

                    # 第一次判断：够长就决定
                    if in_think is None and len(buffer) >= DECISION_LEN:
                        if buffer.lstrip().startswith("<think>"):
                            in_think = True
                            _log("流式：检测到 <think> 开头，进入剥离模式")
                        else:
                            in_think = False

                    # 不在 think 里：边流边吐
                    if in_think is False:
                        yield buffer
                        buffer = ""
                        continue

                    # 在 think 里：只等 </think>
                    if in_think is True:
                        close_idx = buffer.find("</think>")
                        if close_idx != -1:
                            rest = buffer[close_idx + len("</think>"):].lstrip()
                            buffer = rest
                            in_think = False
                            if buffer:
                                yield buffer
                                buffer = ""

                # 流结束，处理残留
                if in_think is None:
                    # 总长度不够判断（很短的回复）
                    if buffer:
                        buffer = _strip_leading_think(buffer)
                        if buffer:
                            yield buffer

                elif in_think is True:
                    # 一直在 think 里没出来（</think> 从未出现）
                    # → 视作失败，吐空响应
                    _log("流式：流结束仍未见到 </think>，视作失败")
                    yield FALLBACK_MESSAGES["empty"]

                elif in_think is False and buffer:
                    yield buffer

                _log(f"✅ stream 完成")
                return

            except asyncio.TimeoutError:
                _log(f"⏱️ stream 超时 attempt {attempt + 1}")
                if attempt == self.max_retries - 1:
                    yield FALLBACK_MESSAGES["timeout"]
                    return
            except Exception as exc:
                error_msg = str(exc)[:200]
                _log(f"❌ stream 错误: {type(exc).__name__}: {error_msg[:100]}")
                if attempt == self.max_retries - 1:
                    yield f"[LLM stream error: {error_msg[:200]}]"
                    return
            finally:
                if stream is not None:
                    try:
                        await stream.close()
                    except Exception:
                        pass

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self._calculate_backoff(attempt, max_wait=10))

        yield FALLBACK_MESSAGES["max_retries"]

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        if model:
            resolved_model = model
        elif role:
            resolved_model = self._resolve_model(role)
        else:
            resolved_model = self.default_model

        async with self._semaphore:
            async for token in self._chat_stream_impl(
                messages,
                resolved_model,
                **kwargs,
            ):
                if token:
                    yield token

    # ======================================================================
    # JSON output
    # ======================================================================

    async def chat_json(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        max_json_attempts = 3
        local_messages = list(messages)

        for attempt in range(max_json_attempts):
            raw = await self.chat(
                local_messages,
                model=model,
                role=role,
                **kwargs,
            )

            if raw.startswith("[") and raw.endswith("]"):
                if attempt == max_json_attempts - 1:
                    return {"error": raw}
                continue

            cleaned = self._extract_json(raw)

            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as exc:
                _log(f"JSON 解析失败 {attempt + 1}: {exc}")
                if attempt == max_json_attempts - 1:
                    return {"content": cleaned, "parse_error": str(exc)}

                local_messages.append({
                    "role": "system",
                    "content": "请只返回有效的 JSON 格式，不要包含任何其他文本。",
                })

        return {"content": raw, "error": "max_json_attempts_exceeded"}

    @staticmethod
    def _extract_json(raw: str) -> str:
        cleaned = raw.strip()

        if "```json" in cleaned:
            parts = cleaned.split("```json", 1)
            if len(parts) > 1:
                cleaned = parts[1].split("```", 1)[0].strip()
        elif "```" in cleaned:
            parts = cleaned.split("```")
            if len(parts) > 1:
                cleaned = parts[1].split("```", 1)[0].strip()

        return cleaned

    # ======================================================================
    # Health check
    # ======================================================================

    async def health_check(self) -> bool:
        for name, client in self._clients.items():
            try:
                await asyncio.wait_for(
                    client.models.list(),
                    timeout=5.0,
                )
                _log(f"✅ health check: {name} OK")
                return True
            except Exception as exc:
                _log(f"⚠️ health check: {name} failed: {exc}")
                continue

        return False