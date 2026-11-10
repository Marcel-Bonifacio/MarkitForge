# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""User configuration file (TOML) for defaults shared by the CLI, GUI and MCP.

Location, first match wins:
1. ``MARKITFORGE_CONFIG`` (a file path)
2. ``$XDG_CONFIG_HOME/markitforge/config.toml`` or ``~/.config/markitforge/config.toml``
   (``%APPDATA%\\markitforge\\config.toml`` on Windows)

Example::

    [defaults]            # conversion options, as passed to convert()
    page_markers = true
    table_format = "auto"

    [limits]              # safety limits (see SECURITY.md)
    max_archive_bytes = 524288000
    max_pdf_pages = 2000
    block_private_networks = true

    [llm]                 # used by describe_images / transcribe_formulas / OCR
    base_url = "http://localhost:11434/v1"   # e.g. Ollama
    model = "llama3.2-vision"
    api_key_env = "OPENAI_API_KEY"           # name of the env var, never the key

Unknown keys are ignored with a warning, so a newer config works with an older
MarkitForge. API keys are never read from the file itself.
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Options that may appear under [defaults] (conversion keyword arguments).
DEFAULT_KEYS = {
    "page_markers",
    "table_format",
    "include_hidden_sheets",
    "include_headers_footers",
    "pdf_backend",
    "keep_data_uris",
    "describe_images",
    "transcribe_formulas",
    "ocr_scanned_pages",
    "ocr_engine",
    "extract_images",
}
# Per-conversion limits (keyword arguments to convert()).
LIMIT_KEYS = {
    "max_archive_bytes",
    "max_archive_member_bytes",
    "max_archive_entries",
    "max_archive_depth",
    "max_compression_ratio",
    "max_inline_bytes",
    "max_llm_calls",
    "max_render_pixels",
    "max_pdf_pages",
    "libreoffice_timeout",
}
# Limits that belong to the MarkitForge(...) constructor (fetching).
INSTANCE_LIMIT_KEYS = {
    "max_download_bytes",
    "fetch_deadline",
    "max_redirects",
    "block_private_networks",
}
LLM_KEYS = {"base_url", "model", "api_key_env", "prompt"}


@dataclass
class Config:
    path: Optional[Path] = None
    defaults: Dict[str, Any] = field(default_factory=dict)
    limits: Dict[str, Any] = field(default_factory=dict)
    instance: Dict[str, Any] = field(default_factory=dict)
    llm: Dict[str, Any] = field(default_factory=dict)

    def conversion_kwargs(self) -> Dict[str, Any]:
        """Defaults plus per-conversion limits, ready for ``convert(**...)``."""
        return {**self.defaults, **self.limits}

    def llm_client(self) -> Any:
        """An OpenAI-compatible client from [llm], or None when not configured."""
        if not self.llm.get("model"):
            return None
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("[llm] is configured but the openai package is missing.")
            return None
        key_env = self.llm.get("api_key_env") or "OPENAI_API_KEY"
        api_key = os.environ.get(key_env) or "not-needed"  # local servers need none
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if self.llm.get("base_url"):
            kwargs["base_url"] = self.llm["base_url"]
        return OpenAI(**kwargs)


def config_path() -> Path:
    env = os.environ.get("MARKITFORGE_CONFIG")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "markitforge" / "config.toml"


def _pick(section: Dict[str, Any], allowed: set, name: str) -> Dict[str, Any]:
    out = {}
    for k, v in section.items():
        if k in allowed:
            out[k] = v
        else:
            logger.warning("Ignoring unknown setting [%s] %s in config.", name, k)
    return out


def load_config(path: Optional[os.PathLike] = None) -> Config:
    """Read the config file. A missing file gives an empty Config."""
    p = Path(path).expanduser() if path else config_path()
    if not p.is_file():
        return Config(path=None)
    if tomllib is None:  # pragma: no cover - Python 3.10
        logger.warning("Config files need Python 3.11+ (tomllib); ignoring %s", p)
        return Config(path=None)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Could not read config file {p}: {exc}") from exc
    limits_raw = dict(data.get("limits", {}))
    instance = {
        k: limits_raw.pop(k) for k in list(limits_raw) if k in INSTANCE_LIMIT_KEYS
    }
    return Config(
        path=p,
        defaults=_pick(dict(data.get("defaults", {})), DEFAULT_KEYS, "defaults"),
        limits=_pick(limits_raw, LIMIT_KEYS, "limits"),
        instance=instance,
        llm=_pick(dict(data.get("llm", {})), LLM_KEYS, "llm"),
    )
