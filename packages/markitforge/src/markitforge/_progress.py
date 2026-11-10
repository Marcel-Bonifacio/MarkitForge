# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Progress reporting and cancellation shared by all converters.

Callers pass two optional keyword arguments to ``convert()``:

- ``progress``: a callable that receives a :class:`ProgressEvent`. Converters
  call :func:`report_progress` as they work (per page, sheet, slide, archive
  entry or batch file). Exceptions raised by the callback are logged and
  ignored, so a broken UI never breaks a conversion.
- ``cancel_event``: anything with ``is_set()`` (e.g. ``threading.Event``).
  Converters call :func:`check_cancelled` between units of work; it raises
  :class:`ConversionCancelled` once the event is set.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ._exceptions import ConversionCancelled

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressEvent:
    stage: str  # "page", "sheet", "slide", "entry", "file", "llm", "start", "done"
    current: int  # 1-based count of finished units (0 at start)
    total: Optional[int] = None  # None when unknown
    message: Optional[str] = None

    @property
    def fraction(self) -> Optional[float]:
        if not self.total:
            return None
        return max(0.0, min(1.0, self.current / self.total))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "current": self.current,
            "total": self.total,
            "message": self.message,
            "fraction": self.fraction,
        }


def report_progress(
    kwargs: Mapping[str, Any],
    stage: str,
    current: int,
    total: Optional[int] = None,
    message: Optional[str] = None,
) -> None:
    """Send a progress event to ``kwargs["progress"]`` if the caller gave one."""
    callback = kwargs.get("progress")
    if callback is None:
        return
    try:
        callback(ProgressEvent(stage, current, total, message))
    except Exception:  # never let a UI callback break a conversion
        logger.debug("progress callback failed", exc_info=True)


def check_cancelled(kwargs: Mapping[str, Any]) -> None:
    """Raise ConversionCancelled when ``kwargs["cancel_event"]`` is set."""
    event = kwargs.get("cancel_event")
    if event is not None and event.is_set():
        raise ConversionCancelled("The conversion was cancelled.")
