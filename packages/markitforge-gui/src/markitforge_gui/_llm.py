# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""AI provider settings for the GUI (OCR, image descriptions, formulas).

The settings live in server memory for the session only. An API key typed
into the page is never written to disk, never logged, never put into the
history and never sent back to the page (the page only learns whether a key
is set). Error messages are scrubbed of the key before they are returned.
"""

import ipaddress
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

__all__ = [
    "LLMSettings",
    "LLMSettingsError",
    "DEFAULT_LOCAL_URL",
    "DEFAULT_LOCAL_MODEL",
    "openai_available",
]

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_MODEL = "llama3.2-vision"
PROVIDERS = ("local", "openai")
_MODEL_RE = re.compile(r"^[\w.:/@+-]{1,200}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{1,500}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")


class LLMSettingsError(ValueError):
    """The settings sent by the page are not valid."""


def openai_available() -> bool:
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LLMSettingsError("Please enter the address of the AI service.")
    url = value.strip()
    parsed = urlparse(url)
    if len(url) > 2048 or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMSettingsError(
            "The AI service address must start with http:// or https://"
        )
    if parsed.username or parsed.password:
        raise LLMSettingsError(
            "Please don't put a user name or password in the address; "
            "use the API key field."
        )
    return url.rstrip("/")


def _check_model(value: Any) -> str:
    if not isinstance(value, str) or not _MODEL_RE.match(value.strip()):
        raise LLMSettingsError(
            "Please enter a model name (letters, numbers and . : / @ + - _)."
        )
    return value.strip()


class LLMSettings:
    """The current provider, address, model and (optional) session API key."""

    def __init__(
        self,
        config_llm: Optional[Dict[str, Any]] = None,
        *,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        cfg = dict(config_llm or {})
        self._lock = threading.Lock()
        self._client_factory = client_factory
        base_url = cfg.get("base_url") if isinstance(cfg.get("base_url"), str) else None
        self.base_url = (base_url or DEFAULT_LOCAL_URL).rstrip("/")
        model = cfg.get("model") if isinstance(cfg.get("model"), str) else None
        self.model = model or DEFAULT_LOCAL_MODEL
        self.provider = "local" if is_loopback_url(self.base_url) else "openai"
        env_name = cfg.get("api_key_env")
        self.key_env = (
            env_name
            if isinstance(env_name, str) and _ENV_RE.match(env_name)
            else "OPENAI_API_KEY"
        )
        self._api_key: Optional[str] = None

    # ---- key handling -----------------------------------------------------

    def _env_key(self) -> Optional[str]:
        value = os.environ.get(self.key_env)
        return value if value else None

    def _effective_key(self) -> Optional[str]:
        if self.provider == "local":
            return None
        return self._api_key or self._env_key()

    def scrub(self, text: str) -> str:
        """Remove any API key from a message before it leaves the server."""
        for key in (self._api_key, self._env_key()):
            if key and len(key) >= 4:
                text = text.replace(key, "***")
        return text

    # ---- public view ------------------------------------------------------

    def public(self) -> Dict[str, Any]:
        with self._lock:
            key_source = None
            if self._api_key:
                key_source = "session"
            elif self._env_key():
                key_source = "env"
            return {
                "provider": self.provider,
                "base_url": self.base_url,
                "model": self.model,
                "key_set": self.provider == "openai" and key_source is not None,
                "key_source": key_source,
                "key_env": self.key_env,
                "is_local": is_loopback_url(self.base_url),
                "openai_available": openai_available(),
                "defaults": {
                    "local_base_url": DEFAULT_LOCAL_URL,
                    "local_model": DEFAULT_LOCAL_MODEL,
                },
            }

    def update(self, body: Dict[str, Any]) -> None:
        provider = body.get("provider", self.provider)
        if provider not in PROVIDERS:
            raise LLMSettingsError("Unknown AI provider.")
        base_url = _check_url(body.get("base_url", self.base_url))
        model = _check_model(body.get("model", self.model))
        new_key = body.get("api_key")
        if new_key is not None:
            if not isinstance(new_key, str):
                raise LLMSettingsError("The API key must be text.")
            new_key = new_key.strip()
            if new_key and not _KEY_RE.match(new_key):
                raise LLMSettingsError(
                    "That API key contains characters keys never have."
                )
        with self._lock:
            url_changed = base_url != self.base_url
            self.provider = provider
            self.base_url = base_url
            self.model = model
            if body.get("clear_key") is True:
                self._api_key = None
            elif new_key:
                self._api_key = new_key
            elif url_changed:
                # Never send a key typed for one service to another one.
                self._api_key = None

    # ---- clients ----------------------------------------------------------

    def client(self, *, timeout: float = 120.0) -> Any:
        """An OpenAI-compatible client for the current settings."""
        with self._lock:
            base_url, provider = self.base_url, self.provider
            key = self._effective_key()
        if provider == "openai" and not key:
            raise LLMSettingsError(
                "Please enter the API key for this AI service in AI options."
            )
        factory = self._client_factory
        if factory is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise LLMSettingsError(
                    "The AI options need the 'openai' package. Install it with: "
                    "pip install openai"
                ) from None
            factory = OpenAI
        # Local servers such as Ollama ignore the key, but the client needs one.
        return factory(
            base_url=base_url,
            api_key=key or "not-needed",
            timeout=timeout,
            max_retries=0 if timeout < 30 else 1,
        )

    def test(self, *, timeout: float = 10.0) -> Dict[str, Any]:
        """List the models (or send a one-word request) to check the settings."""
        client = self.client(timeout=timeout)
        model = self.model
        models: List[str] = []
        listed = False
        list_error: Optional[BaseException] = None
        try:
            page = client.models.list()
            data = getattr(page, "data", page)
            for item in list(data)[:200]:
                ident = getattr(item, "id", None)
                if isinstance(ident, str):
                    models.append(ident[:200])
            listed = True
        except Exception as exc:  # noqa: BLE001 - reported to the page
            list_error = exc
        if listed:
            return {
                "ok": True,
                "models": models[:100],
                "model_found": model in models,
                "message": (
                    f"Connected. {len(models)} model(s) available."
                    + ("" if model in models else f" '{model}' was not in the list.")
                ),
            }
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with OK."}],
                max_tokens=1,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the page
            raise LLMSettingsError(
                self.scrub(
                    f"Could not reach the AI service: {type(exc).__name__}: {exc}"
                )[:500]
            ) from None
        return {
            "ok": True,
            "models": [],
            "model_found": True,
            "message": self.scrub(
                f"Connected. The model answered (listing models failed: "
                f"{type(list_error).__name__})."
            ),
        }
