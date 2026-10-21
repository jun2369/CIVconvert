"""Reading the text of pictures: invoices sent as images (PNG, JPG, TIFF, HEIC …) and scanned PDFs.

A page without a text layer is read by OCR – RapidOCR with the PP-OCR models for Chinese and English, offline
on the CPU – and the table rulings drawn on it are found in the picture (OpenCV). Both are handed to pdfplumber
as if the PDF had them: a character object per letter and a line object per ruling, in page points.
Everything after that – finding the item table, reading its rows by cell or by position, the totals, the
preview – works as for a PDF with text. OCR can misread letters, so what was read this way is marked for review.

Small pictures (screenshots) are enlarged first: PP-OCR reads letters about 30 px high best. A page is read
once; the result is kept for the next time the same file is opened during the conversion.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import settings

log = logging.getLogger("civconvert")

TARGET_SIDE = 2400  # px: a smaller picture is enlarged to about this size (long side), at most 3x
MAX_SIDE = 3200  # px: the most the text detector works at
RENDER_DPI = 300  # a scanned PDF page is drawn at this resolution for OCR (at most MAX_SIDE px)
MIN_SCORE = 0.5  # recognised text less certain than this is left out
MAX_PAGES = 30  # of one file
CACHE_PAGES = 64

_engine: Any = None
_engine_lock = threading.Lock()  # one OCR at a time: it uses the CPU fully
_cache: OrderedDict[tuple[str, int], PageText] = OrderedDict()
_cache_lock = threading.Lock()
_available: bool | None = None


class OcrError(Exception):
    """OCR is not installed or failed."""


@dataclass
class Box:
    """A line of text found on a picture (px)."""

    text: str
    score: float
    x0: float
    top: float
    x1: float
    bottom: float
    spans: list[tuple[float, float]] | None  # x0, x1 of every character of `text`, if OCR gave them


@dataclass
class PageText:
    """What OCR found on a page, as pdfplumber objects (page points)."""

    chars: list[dict[str, Any]]
    lines: list[dict[str, Any]]  # the table rulings


def available() -> bool:
    """Whether the OCR packages can be used here."""
    global _available
    if _available is None:
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
            import rapidocr_onnxruntime  # noqa: F401
            _available = True
        except Exception as e:  # ImportError, or a system library missing (OSError)
            log.warning("OCR is not available (%s: %s) – pictures and scanned PDFs cannot be read", e.__class__.__name__, e)
            _available = False
    return _available


def _get_engine() -> Any:
    global _engine
    if _engine is None:
        import onnxruntime
        from rapidocr_onnxruntime import RapidOCR

        if hasattr(onnxruntime, "disable_telemetry_events"):  # nothing is sent anywhere
            onnxruntime.disable_telemetry_events()
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        threads = max(1, settings.env_int("OCR_THREADS", 2))  # a container sees all the host's cores, not its own share
        _engine = RapidOCR(max_side_len=MAX_SIDE, intra_op_num_threads=threads, inter_op_num_threads=1, print_verbose=False)
    return _engine


def read(picture: Any) -> list[Box]:
    """The lines of text on a picture (PIL image) and where each of their characters is (px of the picture)."""
    import numpy as np
    from PIL import Image

    image = picture.convert("RGB")
    long_side = max(image.size)
    scale = min(3.0, TARGET_SIDE / long_side) if long_side < TARGET_SIDE else 1.0
    if scale > 1.05:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    else:
        scale = 1.0
    bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
    with _engine_lock:
        try:
            found, _ = _get_engine()(bgr, return_word_box=True, text_score=MIN_SCORE)
        except Exception as e:  # onnxruntime / OpenCV errors
            raise OcrError(f"OCR failed ({e.__class__.__name__}: {e})") from e
    boxes = []
    for entry in found or []:
        quad, text, score = entry[0], str(entry[1]), float(entry[2])
        if not text.strip() or score < MIN_SCORE:
            continue
        xs, ys = [p[0] / scale for p in quad], [p[1] / scale for p in quad]
        spans = None
        if len(entry) >= 5 and entry[3] and len(entry[3]) == len(text) and len(entry[4]) == len(text):
            spans = [(min(p[0] for p in cb) / scale, max(p[0] for p in cb) / scale) for cb in entry[3]]
        boxes.append(Box(text, score, min(xs), min(ys), max(xs), max(ys), spans))
    return boxes


# ───────────────────────────── table rulings ─────────────────────────────


def _merge(segments: list[tuple[float, float, float]], near: float, gap: float,
           covered: Callable[[float, float, float], bool]) -> list[tuple[float, float, float]]:
    """Collinear pieces of one line joined: (position, start, end). Pieces close together always; further
    apart when something is drawn over the gap (a stamp, a signature) – not where a table has no line."""
    groups: list[list[tuple[float, float, float]]] = []
    for seg in sorted(segments):
        if groups and seg[0] - groups[-1][-1][0] <= near:
            groups[-1].append(seg)
        else:
            groups.append([seg])
    out: list[tuple[float, float, float]] = []
    for group in groups:
        pos = sum(s[0] for s in group) / len(group)
        cur: list[float] | None = None
        for _, a, b in sorted(group, key=lambda s: s[1]):
            if cur is not None and (a - cur[2] <= gap or covered(pos, cur[2], a)):
                cur[2] = max(cur[2], b)
                continue
            if cur is not None:
                out.append((cur[0], cur[1], cur[2]))
            cur = [pos, a, b]
        if cur is not None:
            out.append((cur[0], cur[1], cur[2]))
    return out


def _snap(hs: list[tuple[float, float, float]], vs: list[tuple[float, float, float]], reach: float) -> tuple[list, list]:
    """Line ends that stop just short of a crossing line are taken to it, so the table's cells close."""
    def nearest(value: float, options: list[float]) -> float:
        best = min(options, key=lambda o: abs(o - value), default=None)
        return best if best is not None and abs(best - value) <= reach else value

    vs = [(x, nearest(a, [y for y, h0, h1 in hs if h0 - reach <= x <= h1 + reach]),
           nearest(b, [y for y, h0, h1 in hs if h0 - reach <= x <= h1 + reach])) for x, a, b in vs]
    hs = [(y, nearest(a, [x for x, v0, v1 in vs if v0 - reach <= y <= v1 + reach]),
           nearest(b, [x for x, v0, v1 in vs if v0 - reach <= y <= v1 + reach])) for y, a, b in hs]
    return hs, vs


def _runs(mask: Any, min_len: int) -> list[tuple[float, float, float]]:
    """Unbroken vertical runs of ink at least `min_len` px long: (x, top, bottom). Run by run, so a stamp or
    a letter touching a ruling does not hide it."""
    import numpy as np

    edges = np.diff(np.pad(mask.astype(np.int8), ((1, 1), (0, 0))), axis=0)
    out = []
    for x in np.flatnonzero(mask.any(axis=0)):
        starts, ends = np.flatnonzero(edges[:, x] == 1), np.flatnonzero(edges[:, x] == -1)
        out.extend((float(x) + 0.5, float(a), float(b)) for a, b in zip(starts, ends) if b - a >= min_len)
    return out


def rulings(picture: Any, text_height: float) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """The straight lines drawn on a picture (table rulings), px: horizontal (y, x0, x1) and vertical (x, top, bottom).
    A ruling is longer than the strokes of the letters; a vertical one has to meet a horizontal one."""
    import cv2
    import numpy as np

    rgb = np.asarray(picture.convert("RGB"))
    gray = np.asarray(picture.convert("L"))
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink[(rgb.max(axis=2) - rgb.min(axis=2)) > 80] = 0  # rulings are black or grey; a red stamp or a blue signature is not one
    th = max(8.0, text_height)
    h_len, v_len = int(max(20, 3 * th)), int(max(10, 0.9 * th))
    thick = max(3.0, 0.35 * th)
    def keep(size: tuple[int, int], block: tuple[int, int]) -> Any:
        """Ink in long strokes of this direction – without filled areas (shaded cells, a stamp's fill)."""
        strokes = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, size))
        return cv2.subtract(strokes, cv2.morphologyEx(strokes, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, block))) > 0

    k = int(thick) + 2  # thicker than this is a filled area, not a ruling
    across, down = keep((h_len, 1), (1, k)), keep((1, v_len), (k, 1))
    painted = gray < 235
    t = int(round(thick))

    def hidden(strip: Any) -> bool:
        """Something is printed over the whole gap (a stamp): hardly a stretch of white paper along it."""
        if strip.size == 0:
            return False
        white = np.concatenate(([0], (~strip.any(axis=0)).astype(np.int8), [0]))
        edges = np.flatnonzero(np.diff(white))
        longest = int((edges[1::2] - edges[::2]).max()) if len(edges) else 0
        return longest <= th and white.mean() <= 0.5

    def covered_h(y: float, a: float, b: float) -> bool:
        return hidden(painted[max(0, int(y) - t): int(y) + t + 1, int(a): int(b) + 1])

    def covered_v(x: float, a: float, b: float) -> bool:
        return hidden(painted[int(a): int(b) + 1, max(0, int(x) - t): int(x) + t + 1].T)

    hs = _merge(_runs(across.T, h_len), thick, 1.5 * th, covered_h)  # transposed: rows become columns
    vs = _merge(_runs(down, v_len), thick, 0.5 * th, covered_v)
    reach = 2 * thick  # a vertical ruling ends at (or crosses) a horizontal one – letters' strokes do not
    hs, vs = _snap(hs, vs, reach)
    hs, vs = _under_stamps(hs, vs, covered_h, covered_v, reach)
    vs = [v for v in vs if any(h[1] - reach <= v[0] <= h[2] + reach and v[1] - reach <= h[0] <= v[2] + reach for h in hs)]
    return hs, vs


def _under_stamps(hs: list[tuple[float, float, float]], vs: list[tuple[float, float, float]],
                  covered_h: Callable[[float, float, float], bool], covered_v: Callable[[float, float, float], bool],
                  reach: float) -> tuple[list, list]:
    """A ruling of the table (its other end meets a crossing ruling) that ends where a stamp covers it goes
    on under the stamp to the next crossing ruling."""
    def longer(segs: list, across: list, covered: Callable[[float, float, float], bool]) -> list:
        out = []
        for pos, a, b in segs:
            def meets(end: float) -> bool:
                return any(s - reach <= pos <= e + reach and abs(p - end) <= reach for p, s, e in across)

            for _ in range(8):  # crossing ruling after crossing ruling, as long as all of it is covered
                after = [p for p, s, e in across if s <= pos <= e and p > b + 1]
                before = [p for p, s, e in across if s <= pos <= e and p < a - 1]
                grew = False
                if after and meets(a) and covered(pos, b, min(after)):
                    b, grew = min(after), True
                if before and meets(b) and covered(pos, max(before), a):
                    a, grew = max(before), True
                if not grew:
                    break
            out.append((pos, a, b))
        return out

    return longer(hs, vs, covered_h), longer(vs, hs, covered_v)


# ───────────────────────────── as pdfplumber objects ─────────────────────────────


def _char(text: str, x0: float, x1: float, top: float, bottom: float, page_number: int, doctop: float, page_bottom: float) -> dict[str, Any]:
    return {
        "text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom, "doctop": doctop + top,
        "y0": page_bottom - bottom, "y1": page_bottom - top, "width": x1 - x0, "height": bottom - top,
        "size": bottom - top, "adv": x1 - x0, "upright": True, "fontname": "OCR", "matrix": (1, 0, 0, 1, x0, page_bottom - bottom),
        "mcid": None, "tag": None, "object_type": "char", "page_number": page_number,
        "ncs": "DeviceGray", "stroking_color": (0,), "non_stroking_color": (0,),
    }


def _chars(box: Box, sx: float, sy: float, ox: float, oy: float, page_number: int, doctop: float, page_bottom: float) -> list[dict[str, Any]]:
    """The characters of a line of text. The letters of a word touch (pdfplumber starts a new word at a gap),
    a space character stands between words."""
    text = box.text
    spans = box.spans or [(box.x0 + i * (box.x1 - box.x0) / len(text), box.x0 + (i + 1) * (box.x1 - box.x0) / len(text)) for i in range(len(text))]
    top, bottom = oy + box.top * sy, oy + box.bottom * sy
    out: list[dict[str, Any]] = []
    last: float | None = None
    space = False
    for ch, (a, b) in zip(text, spans):
        if ch.isspace():
            space = last is not None
            continue
        x0, x1 = ox + a * sx, ox + b * sx
        if last is not None:
            if space:
                out.append(_char(" ", last, max(last, x0), top, bottom, page_number, doctop, page_bottom))
                x0 = max(x0, last)
            else:
                x0 = last
        x1 = max(x1, x0 + 0.05)
        out.append(_char(ch, x0, x1, top, bottom, page_number, doctop, page_bottom))
        last, space = x1, False
    return out


def _line(x0: float, top: float, x1: float, bottom: float, page_number: int, doctop: float, page_bottom: float) -> dict[str, Any]:
    return {
        "x0": x0, "x1": x1, "top": top, "bottom": bottom, "doctop": doctop + top, "y0": page_bottom - bottom, "y1": page_bottom - top,
        "width": x1 - x0, "height": bottom - top, "pts": [(x0, top), (x1, bottom)], "linewidth": 1, "stroke": True, "fill": False,
        "evenodd": False, "stroking_color": (0,), "non_stroking_color": (0,), "mcid": None, "tag": None, "dash": None,
        "path": [("m", (x0, top)), ("l", (x1, bottom))], "object_type": "line", "page_number": page_number,
    }


def page_text(picture: Any, bbox: tuple[float, float, float, float], page_number: int, doctop: float) -> PageText:
    """OCR of a page drawn as `picture`, which covers the page's `bbox` (points)."""
    boxes = read(picture)
    x0, top, x1, bottom = bbox
    sx, sy = (x1 - x0) / picture.width, (bottom - top) / picture.height
    chars = [c for b in boxes for c in _chars(b, sx, sy, x0, top, page_number, doctop, bottom)]
    heights = sorted(b.bottom - b.top for b in boxes)
    hs, vs = rulings(picture, heights[len(heights) // 2] if heights else 20.0)
    lines = [_line(x0 + a * sx, top + y * sy, x0 + b * sx, top + y * sy, page_number, doctop, bottom) for y, a, b in hs]
    lines += [_line(x0 + x * sx, top + a * sy, x0 + x * sx, top + b * sy, page_number, doctop, bottom) for x, a, b in vs]
    return PageText(chars, lines)


# ───────────────────────────── pages of a PDF ─────────────────────────────


def _cached(key: tuple[str, int]) -> PageText | None:
    with _cache_lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
        return found


def _keep(key: tuple[str, int], found: PageText) -> None:
    with _cache_lock:
        _cache[key] = found
        while len(_cache) > CACHE_PAGES:
            _cache.popitem(last=False)


def _needs_ocr(page: Any) -> bool:
    """No text, but something drawn: a scan, a photo, or text drawn as shapes."""
    return not page.chars and bool(page.images or page.curves or page.rects or page.lines)


def _draw(page: Any) -> Any:
    dpi = min(RENDER_DPI, MAX_SIDE * 72 / max(page.width, page.height))
    return page.to_image(resolution=dpi).original


AUTO, ALL, NONE = "auto", "all", "none"


def fill(pdf: Any, source: bytes, pictures: Callable[[], list[Any]] | None = None, mode: str = AUTO,
         max_pages: int = MAX_PAGES) -> tuple[list[int], list[int]]:
    """Read the pages of a pdfplumber PDF that have no text by OCR and give them the text and rulings found
    (in place). mode AUTO: only when no page has text (a picture, a scan) – a PDF with text is read as it
    always was; ALL: also the picture pages of a PDF that has text on other pages; NONE: no OCR.
    `source`: the file's bytes (to know it again). `pictures`: the file's own pictures when the PDF was
    made from a picture – their pixels are read rather than a drawing of the page.
    Returns the numbers of the pages read by OCR, and of those left without text that OCR could read."""
    wanted = [i for i, page in enumerate(pdf.pages) if _needs_ocr(page)]
    if mode == NONE or (mode == AUTO and any(page.chars for page in pdf.pages)):
        return [], [i + 1 for i in wanted]
    done: list[int] = []
    key = ""
    own: list[Any] | None = None
    for i in wanted:
        page = pdf.pages[i]
        if i >= max_pages:
            continue
        key = key or hashlib.sha1(source).hexdigest()
        found = _cached((key, i))
        if found is None:
            if not available():
                break
            try:
                if pictures is not None and own is None:
                    own = pictures()
                picture = own[i] if own and i < len(own) else _draw(page)
                found = page_text(picture, tuple(page.bbox), page.page_number, page.initial_doctop)
            except Exception as e:  # OCR / OpenCV / drawing errors: this page stays without text
                log.warning("OCR of page %d failed: %s: %s", i + 1, e.__class__.__name__, e)
                continue
            _keep((key, i), found)
        objects = dict(page.objects)
        objects["char"] = list(found.chars)
        objects["line"] = list(objects.get("line", [])) + found.lines
        page._objects = objects
        page.flush_cache(["_rect_edges", "_curve_edges", "_edges"])
        page.get_textmap.cache_clear()
        done.append(page.page_number)
    return done, [i + 1 for i in wanted if i + 1 not in done]
