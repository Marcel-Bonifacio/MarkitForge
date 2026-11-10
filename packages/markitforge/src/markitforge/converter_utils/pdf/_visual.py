# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rendering page regions, LLM image captions and formula transcription.

Everything here runs on untrusted PDFs and feeds untrusted LLM answers back
into Markdown, so it is bounded and sanitised:

- regions are rendered on their own (never the whole page) and the
  resolution is lowered to stay within ``max_render_pixels``;
- one :class:`VisualBudget` per document caps LLM calls (images and formulas
  together) and reuses answers for repeated images and formulas;
- captions are reduced to plain alt text and LaTeX answers that could leave
  their ``$$`` block are rejected.
"""

import hashlib
import io
import logging
import math
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Hashable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from ..._base_converter import DocumentAsset
from ..._exceptions import ConversionCancelled
from ..._stream_info import StreamInfo
from .._text_safety import safe_alt_text, safe_latex
from ._layout import Block, TextLine
from ._math import (  # noqa: F401  (re-exported for the converter)
    apply_inline_math,
    display_math_flags,
    inline_math_text,
)

logger = logging.getLogger(__name__)

BBox = Tuple[float, float, float, float]
CaptionFn = Callable[..., Optional[str]]

RENDER_RESOLUTION = 150
MIN_IMAGE_SIDE = 16.0  # points; smaller images are icons, rules or spacers

# Document-wide defaults; 0 means unlimited.
DEFAULT_MAX_RENDER_PIXELS = 4_000_000
DEFAULT_MAX_LLM_CALLS = 50
# Below this resolution a rendered region is useless to a vision model.
MIN_RENDER_RESOLUTION = 36.0
# Renders that need no LLM call (identical images) are capped at this
# multiple of max_llm_calls.
RENDERS_PER_LLM_CALL = 4
# Image export (extract_images) without an LLM renders at most this many
# images per document (0 = unlimited).
DEFAULT_MAX_EXPORT_RENDERS = 1000

FORMULA_PROMPT = (
    "Transcribe the mathematical formula in this image into LaTeX. "
    "Reply with the LaTeX only: no explanation, no Markdown code fences "
    "and no $ or $$ delimiters."
)

# A line is a formula line when this share of its characters use math fonts.
MATH_LINE_RATIO = 0.4
MATH_LINE_MIN_CHARS = 2


class RegionTooLarge(ValueError):
    """A region cannot be rendered within the pixel budget at a useful
    resolution."""


@dataclass
class VisualBudget:
    """Per-document limits and answer caches for the LLM features."""

    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS
    max_render_pixels: int = DEFAULT_MAX_RENDER_PIXELS
    llm_calls: int = 0
    renders: int = 0
    exhausted: bool = False
    # image key (XObject id or PNG digest) -> (asset name, description)
    images: Dict[Hashable, Tuple[str, Optional[str]]] = field(default_factory=dict)
    # PNG digest -> validated LaTeX, "" for no answer, None for rejected
    formulas: Dict[str, Optional[str]] = field(default_factory=dict)
    # Image export without captions: renders done and their cap.
    max_export_renders: int = DEFAULT_MAX_EXPORT_RENDERS
    export_renders: int = 0
    export_exhausted: bool = False
    # Called with the running count after every LLM call is granted.
    on_llm_call: Optional[Callable[[int], None]] = field(default=None, repr=False)

    @property
    def max_renders(self) -> int:
        return self.max_llm_calls * RENDERS_PER_LLM_CALL

    def _stop(self, warnings: List[str]) -> bool:
        if not self.exhausted:
            self.exhausted = True
            warnings.append(
                f"Stopped describing images, transcribing formulas or reading "
                f"scanned pages after "
                f"{self.llm_calls} LLM call(s) (max_llm_calls={self.max_llm_calls}); "
                "the rest of the document was converted without them."
            )
        return False

    def can_render(self, warnings: List[str]) -> bool:
        """Count one render; False once the document's budget is spent."""
        if self.exhausted:
            return False
        if self.max_llm_calls and self.renders >= self.max_renders:
            return self._stop(warnings)
        self.renders += 1
        return True

    def can_call_llm(self, warnings: List[str]) -> bool:
        """Count one LLM call; False once the document's budget is spent."""
        if self.exhausted:
            return False
        if self.max_llm_calls and self.llm_calls >= self.max_llm_calls:
            return self._stop(warnings)
        self.llm_calls += 1
        if self.on_llm_call is not None:
            self.on_llm_call(self.llm_calls)
        return True

    def can_export(self, warnings: List[str]) -> bool:
        """Count one render for image export; False once its cap is hit."""
        if self.export_exhausted:
            return False
        if self.max_export_renders and self.export_renders >= self.max_export_renders:
            self.export_exhausted = True
            warnings.append(
                f"Stopped exporting images after {self.export_renders} "
                "render(s); the remaining images were left out."
            )
            return False
        self.export_renders += 1
        return True


def _clip(page: Any, bbox: BBox) -> Optional[BBox]:
    px0, ptop, px1, pbottom = (float(v) for v in page.bbox)
    x0, top, x1, bottom = bbox
    clipped = (max(x0, px0), max(top, ptop), min(x1, px1), min(bottom, pbottom))
    if clipped[2] - clipped[0] < 1 or clipped[3] - clipped[1] < 1:
        return None
    return clipped


def fit_resolution(
    width: float, height: float, resolution: float, max_pixels: int
) -> float:
    """The highest resolution <= ``resolution`` that keeps a width x height
    point region within ``max_pixels`` (0 = unlimited).

    Raises RegionTooLarge when that would be below MIN_RENDER_RESOLUTION.
    """
    if max_pixels <= 0:
        return resolution
    # One extra point per side covers pdfium rounding the bitmap size up.
    area = (width + 1.0) * (height + 1.0)
    if area * (resolution / 72.0) ** 2 <= max_pixels:
        return resolution
    fitted = 72.0 * math.sqrt(max_pixels / area)
    if fitted < MIN_RENDER_RESOLUTION:
        raise RegionTooLarge(
            f"{width:.0f}x{height:.0f} pt region needs more than "
            f"{max_pixels} pixels even at {MIN_RENDER_RESOLUTION:g} dpi"
        )
    return fitted


def _to_page_frame(
    point: Tuple[float, float], mediabox: BBox, rotation: int
) -> Tuple[float, float]:
    """Raw PDF coordinates -> pdfplumber's (x, top) frame.

    pdfminer lays a page out in its rotated MediaBox (see
    PDFPageInterpreter.process_page) and pdfplumber measures ``top`` from
    the top of that box.
    """
    x, y = point
    mx0, my0, mx1, my1 = mediabox
    if rotation == 90:
        return y - my0, x - mx0
    if rotation == 180:
        return mx1 - x, y - my0
    if rotation == 270:
        return my1 - y, mx1 - x
    return x - mx0, my1 - y


def _visible_frame(pdfium_page: Any) -> BBox:
    """The rendered area (CropBox within MediaBox) in pdfplumber's frame."""
    mx0, my0, mx1, my1 = (float(v) for v in pdfium_page.get_mediabox())
    mediabox = (min(mx0, mx1), min(my0, my1), max(mx0, mx1), max(my0, my1))
    cx0, cy0, cx1, cy1 = (float(v) for v in pdfium_page.get_cropbox())
    cx0, cx1 = max(min(cx0, cx1), mediabox[0]), min(max(cx0, cx1), mediabox[2])
    cy0, cy1 = max(min(cy0, cy1), mediabox[1]), min(max(cy0, cy1), mediabox[3])
    rotation = int(pdfium_page.get_rotation()) % 360
    corners = [
        _to_page_frame(pt, mediabox, rotation)
        for pt in ((cx0, cy0), (cx0, cy1), (cx1, cy0), (cx1, cy1))
    ]
    xs = [c[0] for c in corners]
    tops = [c[1] for c in corners]
    return min(xs), min(tops), max(xs), max(tops)


def _render_region(
    page: Any, bbox: BBox, resolution: float, max_pixels: int
) -> Optional[Any]:
    """Render only ``bbox`` (pdfplumber coordinates) with pdfium.

    pdfplumber's ``crop().to_image()`` rasterises the whole page and crops
    afterwards, which a huge page turns into a memory bomb. Returns None
    when the region is outside the visible page.
    """
    import pypdfium2

    root = page if getattr(page, "is_original", True) else page.root_page
    pdf = root.pdf
    if getattr(pdf, "path", None):
        source: Any = pdf.path
    else:
        pdf.stream.seek(0)
        source = pdf.stream
    document = pypdfium2.PdfDocument(source, password=getattr(pdf, "password", None))
    try:
        pdfium_page = document[root.page_number - 1]
        fx0, ftop, fx1, fbottom = _visible_frame(pdfium_page)
        x0, top, x1, bottom = (
            max(bbox[0], fx0),
            max(bbox[1], ftop),
            min(bbox[2], fx1),
            min(bbox[3], fbottom),
        )
        if x1 - x0 < 1 or bottom - top < 1:
            return None
        dpi = fit_resolution(x1 - x0, bottom - top, resolution, max_pixels)
        bitmap = pdfium_page.render(
            scale=dpi / 72.0,
            # (left, bottom, right, top) to cut off, after rotation; all >= 0
            # because the region lies inside the frame.
            crop=(x0 - fx0, fbottom - bottom, fx1 - x1, top - ftop),
            no_smoothtext=True,
            no_smoothpath=True,
            no_smoothimage=True,
            prefer_bgrx=True,
        )
        return bitmap.to_pil().convert("RGB")
    finally:
        document.close()


def render_region_png(
    page: Any,
    bbox: BBox,
    resolution: float = RENDER_RESOLUTION,
    max_pixels: int = DEFAULT_MAX_RENDER_PIXELS,
) -> Optional[bytes]:
    """PNG bytes of a page region, or None when the region is empty.

    The resolution is lowered so the bitmap stays within ``max_pixels``
    (0 = unlimited); RegionTooLarge is raised when even
    MIN_RENDER_RESOLUTION does not fit.
    """
    clipped = _clip(page, bbox)
    if clipped is None:
        return None
    image = _render_region(page, clipped, resolution, max_pixels)
    if image is None:
        return None
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _ask(
    png: bytes, *, caption_fn: CaptionFn, client: Any, model: str, prompt: Any
) -> Optional[str]:
    info = StreamInfo(mimetype="image/png", extension=".png")
    answer = caption_fn(
        io.BytesIO(png), info, client=client, model=model, prompt=prompt
    )
    return answer.strip() if isinstance(answer, str) else None


def _alt_text(text: str) -> str:
    return safe_alt_text(text)


def _digest(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


def _image_key(img: Dict[str, Any]) -> Optional[Hashable]:
    """Identity of an image XObject, or None for inline images."""
    stream = img.get("stream")
    objid = getattr(stream, "objid", None)
    if objid is None:
        return None
    return ("xobject", objid, getattr(stream, "genno", None))


def _page_image_refs(page: Any) -> List[Tuple[BBox, Optional[Hashable]]]:
    refs: List[Tuple[BBox, Optional[Hashable]]] = []
    for img in page.images:
        try:
            bbox = (
                float(img["x0"]),
                float(img["top"]),
                float(img["x1"]),
                float(img["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        clipped = _clip(page, bbox)
        if clipped is None:
            continue
        if clipped[2] - clipped[0] < MIN_IMAGE_SIDE:
            continue
        if clipped[3] - clipped[1] < MIN_IMAGE_SIDE:
            continue
        refs.append((clipped, _image_key(img)))
    return sorted(refs, key=lambda r: (r[0][1], r[0][0]))


def page_images(page: Any) -> List[BBox]:
    """Bounding boxes of the images on a page worth describing."""
    return [bbox for bbox, _key in _page_image_refs(page)]


def _image_block(bbox: BBox, name: str, description: Optional[str]) -> Block:
    # The link target is always a name we generated (pageN_imgK.png).
    return Block(
        top=bbox[1], x0=bbox[0], markdown=f"![{_alt_text(description or '')}]({name})"
    )


def describe_page_images(
    page: Any,
    page_no: int,
    *,
    client: Any,
    model: str,
    prompt: Optional[str],
    caption_fn: CaptionFn,
    assets: List[DocumentAsset],
    warnings: List[str],
    budget: Optional[VisualBudget] = None,
) -> List[Block]:
    """Caption every image on a page; returns Markdown blocks in place.

    Repeated images (same XObject, or identical rendering) reuse the first
    caption and asset instead of calling the LLM again.
    """
    return page_image_blocks(
        page,
        page_no,
        describe=True,
        export=False,
        client=client,
        model=model,
        prompt=prompt,
        caption_fn=caption_fn,
        assets=assets,
        warnings=warnings,
        budget=budget,
    )


def page_image_blocks(
    page: Any,
    page_no: int,
    *,
    describe: bool,
    export: bool,
    assets: List[DocumentAsset],
    warnings: List[str],
    budget: Optional[VisualBudget] = None,
    client: Any = None,
    model: str = "",
    prompt: Optional[str] = None,
    caption_fn: Optional[CaptionFn] = None,
) -> List[Block]:
    """Image links for a page, as Markdown blocks at the images' positions.

    ``describe`` asks the LLM for alt text (within the budget's LLM calls);
    ``export`` keeps every distinct image as a PNG asset even when it gets
    no description. Without ``export``, only described images are kept, as
    describe_images always did. Repeated images (same XObject, or identical
    rendering) reuse the first asset and caption.
    """
    if budget is None:
        budget = VisualBudget()
    describe = describe and caption_fn is not None
    blocks: List[Block] = []
    description: Optional[str]
    for k, (bbox, key) in enumerate(_page_image_refs(page), start=1):
        if key is not None and key in budget.images:
            name, description = budget.images[key]
            blocks.append(_image_block(bbox, name, description))
            continue
        caption = describe and budget.can_render(warnings)
        if not caption and not (export and budget.can_export(warnings)):
            continue
        name = f"page{page_no}_img{k}.png"
        try:
            png = render_region_png(page, bbox, max_pixels=budget.max_render_pixels)
        except RegionTooLarge as exc:
            logger.warning("Skipped %s: %s", name, exc)
            warnings.append(
                f"Skipped image {k} on page {page_no}: it is too large to "
                f"render ({exc}; see max_render_pixels)."
            )
            continue
        except Exception as exc:
            logger.warning("Could not render %s: %s", name, exc, exc_info=True)
            warnings.append(f"Could not render image {k} on page {page_no}: {exc}")
            continue
        if png is None:
            continue
        digest = ("png", _digest(png))
        if digest in budget.images:
            name, description = budget.images[digest]
            if key is not None:
                budget.images[key] = (name, description)
            blocks.append(_image_block(bbox, name, description))
            continue
        caption = caption and budget.can_call_llm(warnings)
        if not caption and not export:
            continue
        description = None
        if caption:
            try:
                description = _ask(
                    png,
                    caption_fn=caption_fn,  # type: ignore[arg-type]
                    client=client,
                    model=model,
                    prompt=prompt,
                )
            except ConversionCancelled:
                raise
            except Exception as exc:
                logger.warning("Captioning %s failed: %s", name, exc, exc_info=True)
                warnings.append(
                    f"Could not describe image {k} on page {page_no}: {exc}"
                )
        assets.append(
            DocumentAsset(
                name=name,
                mime_type="image/png",
                data=png,
                description=description or None,
                page=page_no,
            )
        )
        budget.images[digest] = (name, description)
        if key is not None:
            budget.images[key] = (name, description)
        blocks.append(_image_block(bbox, name, description))
    return blocks


@dataclass
class MathRegion:
    """Consecutive formula lines, as indexes into the page's line list."""

    start: int
    end: int
    bbox: BBox


def _is_math_line(line: TextLine) -> bool:
    return line.math_chars >= MATH_LINE_MIN_CHARS and line.math_ratio >= MATH_LINE_RATIO


def find_math_regions(
    lines: Sequence[TextLine],
    chars: Optional[Sequence[Mapping[str, Any]]] = None,
) -> List[MathRegion]:
    """Group adjacent formula lines into display regions.

    A line is a formula line when it is set in a math font, or when its
    text is dense with math: Unicode operators, Greek letters and
    super/subscripts. With the page's ``chars``, baseline-shifted scripts
    and Symbol-font letters count too, and script fragments that line
    extraction split off a formula join its region.
    """
    shown = display_math_flags(lines, chars)
    flags = [_is_math_line(line) or shown[k] for k, line in enumerate(lines)]
    regions: List[MathRegion] = []
    i = 0
    while i < len(lines):
        if not flags[i]:
            i += 1
            continue
        j = i + 1
        while (
            j < len(lines)
            and flags[j]
            and lines[j].top - lines[j - 1].bottom < 1.5 * lines[j - 1].height
        ):
            j += 1
        group = lines[i:j]
        bbox = (
            min(l.x0 for l in group) - 2,
            min(l.top for l in group) - 2,
            max(l.x1 for l in group) + 2,
            max(l.bottom for l in group) + 2,
        )
        regions.append(MathRegion(start=i, end=j, bbox=bbox))
        i = j
    return regions


def clean_latex(text: str) -> str:
    """Strip code fences and math delimiters from an LLM's LaTeX answer."""
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    t = t.strip()
    for left, right in (("$$", "$$"), ("\\[", "\\]"), ("$", "$")):
        if t.startswith(left) and t.endswith(right) and len(t) > len(left + right):
            t = t[len(left) : -len(right)].strip()
            break
    return t


def transcribe_math_regions(
    page: Any,
    page_no: int,
    regions: Sequence[MathRegion],
    *,
    client: Any,
    model: str,
    caption_fn: CaptionFn,
    warnings: List[str],
    budget: Optional[VisualBudget] = None,
) -> List[Tuple[MathRegion, Block]]:
    """Ask the LLM for LaTeX for each region.

    Regions that fail, are over budget or get an unsafe answer are skipped,
    so the caller keeps their extracted text.
    """
    if budget is None:
        budget = VisualBudget()
    done: List[Tuple[MathRegion, Block]] = []
    for region in regions:
        if not budget.can_render(warnings):
            break
        latex: Optional[str]
        try:
            png = render_region_png(
                page, region.bbox, max_pixels=budget.max_render_pixels
            )
            if not png:
                continue
            digest = _digest(png)
            if digest in budget.formulas:
                latex = budget.formulas[digest]
            else:
                if not budget.can_call_llm(warnings):
                    break
                answer = _ask(
                    png,
                    caption_fn=caption_fn,
                    client=client,
                    model=model,
                    prompt=FORMULA_PROMPT,
                )
                latex = safe_latex(clean_latex(answer or ""))
                budget.formulas[digest] = latex
        except ConversionCancelled:
            raise
        except RegionTooLarge as exc:
            logger.warning("Skipped a formula on page %s: %s", page_no, exc)
            warnings.append(
                f"Skipped a formula on page {page_no}: it is too large to "
                f"render ({exc}; see max_render_pixels)."
            )
            continue
        except Exception as exc:
            logger.warning(
                "Formula transcription failed on page %s: %s",
                page_no,
                exc,
                exc_info=True,
            )
            warnings.append(f"Could not transcribe a formula on page {page_no}: {exc}")
            continue
        if latex is None:
            warnings.append(
                f"Ignored an unsafe or malformed formula transcription on page "
                f"{page_no}; kept the extracted text."
            )
            continue
        if not latex:
            continue
        block = Block(
            top=region.bbox[1], x0=region.bbox[0], markdown=f"$$\n{latex}\n$$"
        )
        done.append((region, block))
    return done
