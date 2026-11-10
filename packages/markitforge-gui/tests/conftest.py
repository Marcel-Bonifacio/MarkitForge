# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the user's config or write to their data folder."""
    monkeypatch.setenv("MARKITFORGE_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("MARKITFORGE_GUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MARKITFORGE_BLOCK_PRIVATE_URLS", raising=False)
