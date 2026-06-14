"""Headless render QA for generated PPTX decks.

Renders a deck via LibreOffice headless to a PDF, then inspects each page to
flag slides that are likely to look bad when opened by a human: blank slides,
text-heavy slides that probably overflow their placeholder, and visually
crowded slides. This is a best-effort signal layered on top of generation —
it never raises and never blocks document generation if the required binaries
aren't available.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SOFFICE_BIN = shutil.which("soffice") or shutil.which("libreoffice")
PDFTOTEXT_BIN = shutil.which("pdftotext")
PDFTOPPM_BIN = shutil.which("pdftoppm")

CONVERT_TIMEOUT_SECONDS = 60
RENDER_DPI = 60

# Heuristic thresholds — tuned to be conservative (favor false negatives over
# flooding users with false positives).
BLANK_INK_RATIO_THRESHOLD = 0.005   # < 0.5% non-white pixels -> likely blank
DENSE_INK_RATIO_THRESHOLD = 0.35    # > 35% non-white pixels -> visually crowded
DENSE_CHAR_THRESHOLD = 900          # extracted text chars per slide -> likely overflow

# A high ink ratio alone isn't necessarily "crowded" -- intentional full-bleed
# color backgrounds (e.g. title/section slides) are low on text but high on
# ink. Only flag dense_ink when there's also enough text to suggest the
# slide is busy, not just a solid-color design accent.
DENSE_INK_MIN_CHARS = 40


def render_qa_available() -> bool:
    """Whether the binaries needed for render QA are present on this host."""
    return bool(SOFFICE_BIN and PDFTOTEXT_BIN and PDFTOPPM_BIN)


def run_pptx_render_qa(pptx_bytes: bytes, *, timeout: int = CONVERT_TIMEOUT_SECONDS) -> dict:
    """Render `pptx_bytes` headlessly and flag slides likely to render poorly.

    Returns:
        {
          "available": bool,
          "slide_count": int,
          "issues": [{"slide": <1-based int>, "type": "blank" | "dense_text" | "dense_ink",
                       "detail": <human-readable string>}],
          "reason": <str, only present when available is False>,
        }

    This function is intentionally defensive: any missing tool, conversion
    failure, or timeout returns `{"available": False, ...}` rather than
    raising, so callers can treat it as an optional enrichment.
    """
    if not render_qa_available():
        return {"available": False, "reason": "LibreOffice/poppler tools not installed", "issues": []}

    try:
        with tempfile.TemporaryDirectory(prefix="pptx_qa_") as tmpdir:
            tmp = Path(tmpdir)
            pptx_path = tmp / "deck.pptx"
            pptx_path.write_bytes(pptx_bytes)

            try:
                subprocess.run(
                    [
                        SOFFICE_BIN, "--headless", "--norestore",
                        "--convert-to", "pdf", "--outdir", str(tmp), str(pptx_path),
                    ],
                    check=True, capture_output=True, timeout=timeout,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                return {"available": False, "reason": f"PDF conversion failed: {exc}", "issues": []}

            pdf_path = tmp / "deck.pdf"
            if not pdf_path.exists():
                return {"available": False, "reason": "PDF conversion produced no output", "issues": []}

            pages_text: list[str] = []
            try:
                result = subprocess.run(
                    [PDFTOTEXT_BIN, "-layout", str(pdf_path), "-"],
                    check=True, capture_output=True, timeout=timeout,
                )
                raw = result.stdout.decode("utf-8", errors="ignore")
                # pdftotext separates pages with form-feed characters.
                pages_text = raw.split("\f")
                if pages_text and pages_text[-1] == "":
                    pages_text = pages_text[:-1]
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.warning("pdftotext failed during PPTX render QA", exc_info=True)

            try:
                subprocess.run(
                    [PDFTOPPM_BIN, "-png", "-r", str(RENDER_DPI), str(pdf_path), str(tmp / "page")],
                    check=True, capture_output=True, timeout=timeout,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.warning("pdftoppm failed during PPTX render QA", exc_info=True)

            image_paths = sorted(tmp.glob("page-*.png"))
            slide_count = max(len(pages_text), len(image_paths))

            issues: list[dict] = []
            for idx in range(slide_count):
                slide_num = idx + 1
                text = pages_text[idx].strip() if idx < len(pages_text) else ""
                char_count = len(text)

                ink_ratio = _ink_ratio(image_paths[idx]) if idx < len(image_paths) else None

                if char_count == 0 and (ink_ratio is None or ink_ratio < BLANK_INK_RATIO_THRESHOLD):
                    issues.append({
                        "slide": slide_num,
                        "type": "blank",
                        "detail": "Slide appears to have no visible text or content.",
                    })

                if char_count > DENSE_CHAR_THRESHOLD:
                    issues.append({
                        "slide": slide_num,
                        "type": "dense_text",
                        "detail": (
                            f"Slide has ~{char_count} characters of extracted text, which is likely "
                            "too much for one slide and may overflow its placeholder."
                        ),
                    })

                if (
                    ink_ratio is not None
                    and ink_ratio > DENSE_INK_RATIO_THRESHOLD
                    and char_count >= DENSE_INK_MIN_CHARS
                ):
                    issues.append({
                        "slide": slide_num,
                        "type": "dense_ink",
                        "detail": (
                            f"Slide is visually crowded (~{ink_ratio:.0%} of the slide is non-blank); "
                            "consider trimming content or splitting into multiple slides."
                        ),
                    })

            return {"available": True, "slide_count": slide_count, "issues": issues}
    except Exception as exc:  # pragma: no cover - belt-and-braces, QA must never break generation
        logger.exception("Unexpected error during PPTX render QA")
        return {"available": False, "reason": f"Unexpected error: {exc}", "issues": []}


def _ink_ratio(image_path: Path) -> float | None:
    """Fraction of pixels that differ materially from the slide's dominant
    (background) color in `image_path`, or None on failure.

    Slides can use any background color/theme (e.g. a warm cream
    `F7F1EE` ~242 luma), not just pure white. A fixed "< 250 = ink" cutoff
    would count an entire colored background as "ink", making every slide
    in a themed deck register as ~100% crowded. Instead, find the most
    common grayscale value (the background) and count pixels that deviate
    from it by more than a small tolerance as "ink" (text, images, shapes).
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(image_path) as img:
            gray = img.convert("L")
            histogram = gray.histogram()
            total = sum(histogram)
            if not total:
                return None
            bg_value = max(range(256), key=lambda v: histogram[v])
            tolerance = 12
            non_bg = sum(
                count
                for value, count in enumerate(histogram)
                if abs(value - bg_value) > tolerance
            )
            return non_bg / total
    except Exception:
        logger.warning("Failed to compute ink ratio for %s", image_path, exc_info=True)
        return None
