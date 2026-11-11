# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PDF progress and cancellation, image export and OCR of scanned pages.

Fixtures are built with reportlab and PIL in a temporary directory; the
LLM is a fake OpenAI-shaped client, and the tesseract tests are skipped when
tesseract is not installed.
"""

import io
import re
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from markitforge import (
    ConversionCancelled,
    MarkitForge,
    MissingDependencyException,
    StreamInfo,
)
from markitforge.converters._pdf_converter import PdfConverter
from markitforge.converter_utils.pdf import _ocr
from markitforge.converter_utils.pdf._ocr import clean_ocr_text

pytest.importorskip("reportlab")
pytest.importorskip("PIL")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# -- fake LLM client --------------------------------------------------------


class _FakeCompletions:
    def __init__(self, reply: Any):
        self.reply = reply
        self.calls: List[Dict[str, Any]] = []

    def create(self, model: str, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"model": model, "messages": messages})
        reply = self.reply(len(self.calls)) if callable(self.reply) else self.reply
        message = SimpleNamespace(content=reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeLlmClient:
    """Shaped like openai.OpenAI for client.chat.completions.create()."""

    def __init__(self, reply: Any):
        self.chat = SimpleNamespace(completions=_FakeCompletions(reply))

    @property
    def calls(self) -> List[Dict[str, Any]]:
        return self.chat.completions.calls


def _llm(client: FakeLlmClient) -> Dict[str, Any]:
    return {"llm_client": client, "llm_model": "fake-vision"}


def _prompt_of(call: Dict[str, Any]) -> str:
    return call["messages"][0]["content"][0]["text"]


# -- fixture builders ---------------------------------------------------------


def _png(color: Tuple[int, int, int], size: int = 48) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _text_pdf(tmp_path: Path, pages: int = 3) -> Path:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "mf_roadmap_text.pdf"
    c = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    for n in range(1, pages + 1):
        c.setFont("Helvetica", 11)
        c.drawString(72, 720, f"Page {n} says something ordinary about item {n}.")
        c.showPage()
    c.save()
    return path


def _image_pdf(tmp_path: Path) -> Path:
    """Page 1: text, red image, text, blue image. Page 2: the red image again."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    red, blue = _png((220, 20, 20)), _png((20, 20, 220))
    path = tmp_path / "mf_roadmap_images.pdf"
    c = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "Text before the red picture.")
    c.drawImage(ImageReader(io.BytesIO(red)), 72, 600, 100, 100)
    c.drawString(72, 570, "Text between the pictures.")
    c.drawImage(ImageReader(io.BytesIO(blue)), 72, 450, 100, 100)
    c.drawString(72, 420, "Text after the blue picture.")
    c.showPage()
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "The red picture once more.")
    c.drawImage(ImageReader(io.BytesIO(red)), 72, 600, 100, 100)
    c.showPage()
    c.save()
    return path


def _scan_png(text: str, seed: int = 0) -> bytes:
    """A white 'scanned page' with large black text."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1240, 1754), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=72)
    draw.text((100, 200), text, fill=(0, 0, 0), font=font)
    # A distinct speck per seed so different pages render differently.
    draw.rectangle((50 + seed * 10, 1650, 56 + seed * 10, 1656), fill=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _scan_pdf(
    tmp_path: Path, scans: List[Tuple[str, int]], with_image: bool = False
) -> Path:
    """Page 1 has a text layer (and, ``with_image``, a small picture); every
    further page is one full-page image."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    path = tmp_path / f"mf_roadmap_scan_{len(scans)}_{int(with_image)}.pdf"
    width, height = A4
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setFont("Helvetica", 11)
    c.drawString(72, height - 72, "This first page has a real text layer.")
    if with_image:
        c.drawImage(
            ImageReader(io.BytesIO(_png((220, 20, 20)))), 72, height - 250, 100, 100
        )
    c.showPage()
    for text, seed in scans:
        c.drawImage(ImageReader(io.BytesIO(_scan_png(text, seed))), 0, 0, width, height)
        c.showPage()
    c.save()
    return path


def _convert(path: Path, **kwargs: Any) -> Any:
    return MarkitForge().convert(str(path), **kwargs)


# -- progress and cancellation -----------------------------------------------


class TestProgressAndCancel:
    def test_page_events(self, tmp_path: Path) -> None:
        events: List[Any] = []
        _convert(_text_pdf(tmp_path), progress=events.append)
        pages = [(e.current, e.total) for e in events if e.stage == "page"]
        assert pages == [(1, 3), (2, 3), (3, 3)]

    def test_page_events_on_pdfminer_backend(self, tmp_path: Path) -> None:
        events: List[Any] = []
        _convert(_text_pdf(tmp_path), pdf_backend="pdfminer", progress=events.append)
        pages = [(e.current, e.total) for e in events if e.stage == "page"]
        assert pages == [(1, 3), (2, 3), (3, 3)]

    def test_broken_callback_does_not_break_conversion(self, tmp_path: Path) -> None:
        def boom(event: Any) -> None:
            raise RuntimeError("UI went away")

        result = _convert(_text_pdf(tmp_path), progress=boom)
        assert "Page 3 says" in result.markdown

    def test_cancel_mid_document(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        seen: List[int] = []

        def progress(event: Any) -> None:
            if event.stage == "page":
                seen.append(event.current)
                if event.current == 1:
                    cancel.set()

        with pytest.raises(ConversionCancelled):
            _convert(_text_pdf(tmp_path), progress=progress, cancel_event=cancel)
        assert seen == [1]  # page 2 never started

    def test_cancel_before_start(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        cancel.set()
        for backend in ("auto", "pdfplumber", "pdfminer"):
            with pytest.raises(ConversionCancelled):
                _convert(_text_pdf(tmp_path), cancel_event=cancel, pdf_backend=backend)

    def test_cancel_is_not_swallowed_by_pdfminer_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def cancelled(*args: Any, **kwargs: Any) -> Any:
            raise ConversionCancelled("stop")

        monkeypatch.setattr(PdfConverter, "_process_page", cancelled)
        with pytest.raises(ConversionCancelled):
            _convert(_text_pdf(tmp_path))

    def test_llm_events(self, tmp_path: Path) -> None:
        events: List[Any] = []
        client = FakeLlmClient("A coloured square")
        _convert(
            _image_pdf(tmp_path),
            describe_images=True,
            progress=events.append,
            **_llm(client),
        )
        llm = [e.current for e in events if e.stage == "llm"]
        assert llm == [1, 2] and len(client.calls) == 2


# -- image export -------------------------------------------------------------


class TestExtractImages:
    def test_assets_names_and_links(self, tmp_path: Path) -> None:
        result = _convert(_image_pdf(tmp_path), extract_images=True)
        names = [a.name for a in result.assets]
        assert names == ["page1_img1.png", "page1_img2.png"]
        for asset in result.assets:
            assert asset.mime_type == "image/png"
            assert asset.data.startswith(PNG_SIGNATURE)
            assert asset.page == 1
            assert asset.description is None
        md = result.markdown
        before = md.index("Text before the red picture.")
        red = md.index("![](page1_img1.png)")
        between = md.index("Text between the pictures.")
        blue = md.index("![](page1_img2.png)")
        after = md.index("Text after the blue picture.")
        assert before < red < between < blue < after

    def test_repeated_image_is_deduplicated(self, tmp_path: Path) -> None:
        result = _convert(_image_pdf(tmp_path), extract_images=True)
        assert len(result.assets) == 2
        assert "![](page1_img1.png)" in result.pages[1].markdown
        assert result.markdown.count("![](page1_img1.png)") == 2

    def test_default_output_is_unchanged(self, tmp_path: Path) -> None:
        path = _image_pdf(tmp_path)
        default = _convert(path)
        off = _convert(path, extract_images=False)
        assert off.markdown == default.markdown
        assert [p.markdown for p in off.pages] == [p.markdown for p in default.pages]
        assert default.assets == [] and "![" not in default.markdown

    def test_names_are_safe(self, tmp_path: Path) -> None:
        result = _convert(_image_pdf(tmp_path), extract_images=True)
        for asset in result.assets:
            assert re.fullmatch(r"page\d+_img\d+\.png", asset.name)

    def test_with_captions(self, tmp_path: Path) -> None:
        client = FakeLlmClient("A coloured square")
        result = _convert(
            _image_pdf(tmp_path),
            extract_images=True,
            describe_images=True,
            **_llm(client),
        )
        assert len(client.calls) == 2  # third occurrence reuses the first
        assert len(result.assets) == 2
        assert result.markdown.count("![A coloured square](page1_img1.png)") == 2
        assert "![A coloured square](page1_img2.png)" in result.markdown
        assert all(a.description == "A coloured square" for a in result.assets)

    def test_images_past_llm_budget_are_still_exported(self, tmp_path: Path) -> None:
        client = FakeLlmClient("A coloured square")
        result = _convert(
            _image_pdf(tmp_path),
            extract_images=True,
            describe_images=True,
            max_llm_calls=1,
            **_llm(client),
        )
        assert len(client.calls) == 1
        assert [a.name for a in result.assets] == ["page1_img1.png", "page1_img2.png"]
        assert "![A coloured square](page1_img1.png)" in result.markdown
        assert "![](page1_img2.png)" in result.markdown
        assert any("max_llm_calls=1" in w for w in result.warnings)

    def test_describe_only_behaviour_kept(self, tmp_path: Path) -> None:
        client = FakeLlmClient("A coloured square")
        result = _convert(
            _image_pdf(tmp_path), describe_images=True, max_llm_calls=1, **_llm(client)
        )
        # Without extract_images, images past the budget are left out.
        assert [a.name for a in result.assets] == ["page1_img1.png"]
        assert "page1_img2.png" not in result.markdown

    def test_caption_injection_stays_in_alt_text(self, tmp_path: Path) -> None:
        client = FakeLlmClient("Red](https://attacker.example/x.png) <img src=x>")
        result = _convert(
            _image_pdf(tmp_path),
            extract_images=True,
            describe_images=True,
            **_llm(client),
        )
        md = result.markdown
        links = re.findall(r"!\[([^\]]*)\]\(([^)]*)\)", md)
        assert {target for _alt, target in links} == {
            "page1_img1.png",
            "page1_img2.png",
        }
        assert "<img" not in md and "](https" not in md

    def test_pixel_budget_applies(self, tmp_path: Path) -> None:
        result = _convert(
            _image_pdf(tmp_path), extract_images=True, max_render_pixels=100
        )
        assert result.assets == []
        assert any("max_render_pixels" in w for w in result.warnings)

    def test_form_pages_get_images(self, tmp_path: Path) -> None:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        path = tmp_path / "mf_roadmap_form.pdf"
        c = canvas.Canvas(str(path), pagesize=letter, invariant=1)
        c.setFont("Helvetica", 10)
        y = 720
        for row in range(6):
            for col, x in enumerate((72, 200, 330, 460)):
                c.drawString(x, y, f"R{row}C{col}")
            y -= 18
        c.drawImage(ImageReader(io.BytesIO(_png((0, 160, 0)))), 72, 400, 100, 100)
        c.showPage()
        c.save()
        result = _convert(path, extract_images=True)
        assert [a.name for a in result.assets] == ["page1_img1.png"]
        assert "![](page1_img1.png)" in result.markdown


# -- OCR ----------------------------------------------------------------------

OCR_REPLY = "# Scanned Notice\n\n- first point\n- second point"


class TestOcrLlm:
    def test_scanned_page_is_recognised(self, tmp_path: Path) -> None:
        client = FakeLlmClient(OCR_REPLY)
        path = _scan_pdf(tmp_path, [("SCANNED NOTICE", 0)])
        result = _convert(
            path, ocr_scanned_pages=True, page_markers=True, **_llm(client)
        )
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["model"] == "fake-vision"
        assert "scanned document" in _prompt_of(call)
        image_url = call["messages"][0]["content"][1]["image_url"]["url"]
        assert image_url.startswith("data:image/png;base64,")
        assert result.metadata["ocr_pages"] == [2]
        assert result.metadata["ocr_engine"] == "llm"
        assert result.pages[1].markdown == OCR_REPLY
        md = result.markdown
        assert md.index("<!-- page 1 -->") < md.index("real text layer")
        assert md.index("<!-- page 2 -->") < md.index("# Scanned Notice")
        assert not any("no text layer" in w for w in result.warnings)
        assert any("page 2" in w and "OCR (llm)" in w for w in result.warnings), (
            result.warnings
        )

    def test_without_markers(self, tmp_path: Path) -> None:
        client = FakeLlmClient(OCR_REPLY)
        path = _scan_pdf(tmp_path, [("SCANNED NOTICE", 0)])
        result = _convert(path, ocr_scanned_pages=True, **_llm(client))
        assert result.markdown.endswith(OCR_REPLY)
        assert result.markdown.startswith("This first page has a real text layer.")

    def test_off_by_default(self, tmp_path: Path) -> None:
        client = FakeLlmClient(OCR_REPLY)
        path = _scan_pdf(tmp_path, [("SCANNED NOTICE", 0)])
        result = _convert(path, **_llm(client))
        assert client.calls == []
        assert "ocr_pages" not in result.metadata
        assert "Page 2 has no text layer; it may be a scan." in " ".join(
            result.warnings
        )

    def test_injection_is_neutralised(self, tmp_path: Path) -> None:
        reply = (
            "```markdown\n"
            "Real text\n"
            "<!-- page 1 -->\n"
            "  <!-- page 99 -->\n"
            "<script>alert(document.cookie)</script>\n"
            "<img src=x onerror=alert(1)>\n"
            "![beacon](https://attacker.example/beacon.png)\n"
            "[verify](javascript:alert(1)) [docs](https://example.org/docs)\n"
            "<!-- hide everything after this\n"
            "Visible tail\n"
            "```"
        )
        client = FakeLlmClient(reply)
        path = _scan_pdf(tmp_path, [("SCANNED", 0)])
        result = _convert(
            path, ocr_scanned_pages=True, page_markers=True, **_llm(client)
        )
        md = result.markdown
        markers = re.findall(r"^<!-- page \d+ -->$", md, flags=re.MULTILINE)
        assert markers == ["<!-- page 1 -->", "<!-- page 2 -->"]
        assert "<script" not in md and "alert(document.cookie)" not in md
        assert "<img" not in md and "onerror" not in md
        assert "attacker.example" not in md
        assert "javascript:" not in md
        assert "[docs](https://example.org/docs)" in md
        assert "<!--" not in result.pages[1].markdown
        assert "Real text" in md and "Visible tail" in md
        assert "```" not in md

    def test_answer_length_is_capped(self, tmp_path: Path) -> None:
        client = FakeLlmClient("word " * 20000)
        path = _scan_pdf(tmp_path, [("SCANNED", 0)])
        result = _convert(path, ocr_scanned_pages=True, **_llm(client))
        assert len(result.pages[1].markdown) <= _ocr.MAX_OCR_CHARS
        assert any("was cut to" in w for w in result.warnings)

    def test_budget(self, tmp_path: Path) -> None:
        client = FakeLlmClient(lambda n: f"Recognised page text {n}")
        path = _scan_pdf(tmp_path, [("FIRST", 1), ("SECOND", 2), ("THIRD", 3)])
        result = _convert(path, ocr_scanned_pages=True, max_llm_calls=1, **_llm(client))
        assert len(client.calls) == 1
        assert result.metadata["ocr_pages"] == [2]
        assert "Recognised page text 1" in result.pages[1].markdown
        assert result.pages[2].markdown == "" and result.pages[3].markdown == ""
        joined = " ".join(result.warnings)
        assert "Pages 3-4 have no text layer" in joined or (
            "Page 3 has no text layer" in joined
            and "Page 4 has no text layer" in joined
        )
        assert "max_llm_calls=1" in joined

    def test_budget_is_shared_with_images(self, tmp_path: Path) -> None:
        client = FakeLlmClient("Shared answer")
        path = _scan_pdf(tmp_path, [("FIRST", 1)], with_image=True)
        result = _convert(
            path,
            ocr_scanned_pages=True,
            describe_images=True,
            max_llm_calls=1,
            **_llm(client),
        )
        # The caption on page 1 takes the only LLM call; page 2 is not OCR'd.
        assert len(client.calls) == 1
        assert result.metadata["ocr_pages"] == []
        assert [a.name for a in result.assets] == ["page1_img1.png"]
        joined = " ".join(result.warnings)
        assert "Page 2 has no text layer" in joined
        assert "reading scanned pages" in joined

    def test_ocr_page_scan_is_not_captioned(self, tmp_path: Path) -> None:
        def reply(n: int) -> str:
            prompt = _prompt_of(client.calls[-1])
            return OCR_REPLY if "scanned document" in prompt else "A red square"

        client = FakeLlmClient(reply)
        path = _scan_pdf(tmp_path, [("FIRST", 1)], with_image=True)
        result = _convert(
            path,
            ocr_scanned_pages=True,
            describe_images=True,
            extract_images=True,
            **_llm(client),
        )
        # One caption (page 1) and one OCR call (page 2); no caption of the scan.
        prompts = [_prompt_of(c) for c in client.calls]
        assert len(prompts) == 2
        assert sum("scanned document" in p for p in prompts) == 1
        assert "![A red square](page1_img1.png)" in result.pages[0].markdown
        assert result.pages[1].markdown == OCR_REPLY + "\n\n![](page2_img1.png)"
        scan = [a for a in result.assets if a.page == 2]
        assert len(scan) == 1 and scan[0].description is None

    def test_ocr_page_without_export_has_no_image(self, tmp_path: Path) -> None:
        client = FakeLlmClient(OCR_REPLY)
        path = _scan_pdf(tmp_path, [("FIRST", 1)])
        result = _convert(
            path, ocr_scanned_pages=True, describe_images=True, **_llm(client)
        )
        assert len(client.calls) == 1
        assert result.pages[1].markdown == OCR_REPLY
        assert result.assets == []

    def test_identical_scans_reuse_the_answer(self, tmp_path: Path) -> None:
        client = FakeLlmClient(lambda n: f"Answer {n}")
        path = _scan_pdf(tmp_path, [("SAME", 0), ("SAME", 0)])
        result = _convert(path, ocr_scanned_pages=True, **_llm(client))
        assert len(client.calls) == 1
        assert result.metadata["ocr_pages"] == [2, 3]
        assert result.pages[1].markdown == result.pages[2].markdown == "Answer 1"

    def test_empty_answer_keeps_scan_warning(self, tmp_path: Path) -> None:
        client = FakeLlmClient("   ")
        path = _scan_pdf(tmp_path, [("SCANNED", 0)])
        result = _convert(path, ocr_scanned_pages=True, **_llm(client))
        assert result.metadata["ocr_pages"] == []
        assert any("Page 2 has no text layer" in w for w in result.warnings)

    def test_failing_client_keeps_scan_warning(self, tmp_path: Path) -> None:
        class Broken:
            def create(self, **kwargs: Any) -> Any:
                raise RuntimeError("service down")

        client = SimpleNamespace(chat=SimpleNamespace(completions=Broken()))
        path = _scan_pdf(tmp_path, [("SCANNED", 0)])
        result = _convert(
            path, ocr_scanned_pages=True, llm_client=client, llm_model="m"
        )
        joined = " ".join(result.warnings)
        assert "Could not OCR page 2: service down" in joined
        assert "Page 2 has no text layer" in joined

    def test_cancel_during_ocr_document(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        client = FakeLlmClient(lambda n: cancel.set() or f"Answer {n}")
        path = _scan_pdf(tmp_path, [("FIRST", 1), ("SECOND", 2)])
        with pytest.raises(ConversionCancelled):
            _convert(path, ocr_scanned_pages=True, cancel_event=cancel, **_llm(client))
        assert len(client.calls) == 1

    def test_extract_images_on_ocr_page(self, tmp_path: Path) -> None:
        client = FakeLlmClient(OCR_REPLY)
        path = _scan_pdf(tmp_path, [("SCANNED", 0)])
        result = _convert(
            path, ocr_scanned_pages=True, extract_images=True, **_llm(client)
        )
        assert [a.name for a in result.assets] == ["page2_img1.png"]
        assert result.pages[1].markdown == OCR_REPLY + "\n\n![](page2_img1.png)"

    def test_llm_engine_without_client_warns(self, tmp_path: Path) -> None:
        path = _scan_pdf(tmp_path, [("SCANNED", 0)])
        result = _convert(path, ocr_scanned_pages=True, ocr_engine="llm")
        joined = " ".join(result.warnings)
        assert "ocr_engine='llm' needs llm_client and llm_model" in joined
        assert "Page 2 has no text layer" in joined
        assert result.metadata["ocr_pages"] == []

    def test_auto_without_any_engine_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_ocr, "tesseract_available", lambda: False)
        path = _scan_pdf(tmp_path, [("SCANNED", 0)])
        result = _convert(path, ocr_scanned_pages=True)
        joined = " ".join(result.warnings)
        assert "found no OCR engine" in joined
        assert "Page 2 has no text layer" in joined

    def test_text_pdf_with_ocr_on_is_unchanged(self, tmp_path: Path) -> None:
        client = FakeLlmClient(OCR_REPLY)
        path = _text_pdf(tmp_path)
        assert (
            _convert(path, ocr_scanned_pages=True, **_llm(client)).markdown
            == _convert(path).markdown
        )
        assert client.calls == []

    def test_bad_engine(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            PdfConverter().convert(
                io.BytesIO(_text_pdf(tmp_path).read_bytes()),
                StreamInfo(extension=".pdf"),
                ocr_scanned_pages=True,
                ocr_engine="magic",
            )


class TestOcrTesseract:
    def test_missing_tesseract_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_ocr, "tesseract_available", lambda: False)
        with pytest.raises(MissingDependencyException, match="pytesseract"):
            PdfConverter().convert(
                io.BytesIO(_scan_pdf(tmp_path, [("X", 0)]).read_bytes()),
                StreamInfo(extension=".pdf"),
                ocr_scanned_pages=True,
                ocr_engine="tesseract",
            )

    @pytest.mark.skipif(
        shutil.which("tesseract") is None, reason="tesseract is not installed"
    )
    def test_tesseract_reads_the_scan(self, tmp_path: Path) -> None:
        pytest.importorskip("pytesseract")
        path = _scan_pdf(tmp_path, [("HELLO SCANNER", 0)])
        result = _convert(path, ocr_scanned_pages=True, ocr_engine="tesseract")
        assert result.metadata["ocr_pages"] == [2]
        assert result.metadata["ocr_engine"] == "tesseract"
        assert "HELLO" in result.pages[1].markdown.upper()


# -- sanitiser ----------------------------------------------------------------


class TestCleanOcrText:
    def test_plain_markdown_is_kept(self) -> None:
        text = "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n1. one\n2. two"
        assert clean_ocr_text(text) == (text, False)

    def test_comparisons_survive(self) -> None:
        assert clean_ocr_text("if a < b and c > d")[0] == "if a < b and c > d"

    def test_controls_and_bidi_removed(self) -> None:
        cleaned, _ = clean_ocr_text("ab\x00c‮d​e\r\nf")
        assert cleaned == "abcde\nf"

    def test_nested_markup(self) -> None:
        cleaned, _ = clean_ocr_text("<scr<script></script>ipt>alert(1)</script>")
        assert "<script" not in cleaned

    def test_reference_definitions_dropped(self) -> None:
        cleaned, _ = clean_ocr_text("see [x]\n\n[x]: https://attacker.example/")
        assert "attacker" not in cleaned

    def test_truncation(self) -> None:
        cleaned, cut = clean_ocr_text("line\n" * 100, limit=50)
        assert cut and len(cleaned) <= 50
