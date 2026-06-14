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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import get_settings

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
TINY_TEXT_CHAR_THRESHOLD = 1400     # extreme extracted chars -> likely tiny/unreadable text

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

            inspected = _inspect_rendered_slides_parallel(pages_text, image_paths, slide_count)
            issues: list[dict] = []
            metrics: list[dict] = []
            for result in inspected:
                issues.extend(result["issues"])
                metrics.append(result["metrics"])

            return {
                "available": True,
                "slide_count": slide_count,
                "issues": sorted(issues, key=lambda item: (item.get("slide") or 0, item.get("type") or "")),
                "metrics": metrics,
                "parallel": True,
                "workers": min(slide_count or 1, max(1, get_settings().max_pptx_render_qa_workers)),
            }
    except Exception as exc:  # pragma: no cover - belt-and-braces, QA must never break generation
        logger.exception("Unexpected error during PPTX render QA")
        return {"available": False, "reason": f"Unexpected error: {exc}", "issues": []}


def _inspect_rendered_slides_parallel(pages_text: list[str], image_paths: list[Path], slide_count: int) -> list[dict]:
    """Inspect rendered slides concurrently after a single deck render.

    LibreOffice/PDF conversion is the expensive serialized step. Once pages are
    available, each slide can be inspected independently, so this fans out the
    per-slide image/text heuristics and returns results in slide order.
    """
    if slide_count <= 0:
        return []
    max_workers = min(slide_count, max(1, get_settings().max_pptx_render_qa_workers))
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for idx in range(slide_count):
            text = pages_text[idx].strip() if idx < len(pages_text) else ""
            image_path = image_paths[idx] if idx < len(image_paths) else None
            futures[pool.submit(_inspect_rendered_slide, idx + 1, text, image_path)] = idx
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception:  # pragma: no cover - per-slide QA must not break deck generation
                logger.warning("Slide render QA failed for slide %s", idx + 1, exc_info=True)
                results[idx] = {
                    "metrics": {"slide": idx + 1, "char_count": 0, "ink_ratio": None},
                    "issues": [],
                }
    return [results[idx] for idx in range(slide_count)]


def _inspect_rendered_slide(slide_num: int, text: str, image_path: Path | None) -> dict:
    char_count = len(text or "")
    ink_ratio = _ink_ratio(image_path) if image_path is not None else None

    issues: list[dict] = []
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

    if char_count > TINY_TEXT_CHAR_THRESHOLD:
        issues.append({
            "slide": slide_num,
            "type": "tiny_text_risk",
            "detail": (
                f"Slide has ~{char_count} extracted characters; if it fit visually, "
                "the renderer likely had to shrink text below comfortable reading size."
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

    return {
        "metrics": {
            "slide": slide_num,
            "char_count": char_count,
            "ink_ratio": ink_ratio,
        },
        "issues": issues,
    }


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
