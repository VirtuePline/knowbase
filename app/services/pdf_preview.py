"""原始 PDF 的页码与预览。"""
from __future__ import annotations

from io import BytesIO
from functools import lru_cache
from math import isfinite, log
from pathlib import Path
import re
from threading import Lock

from app.core.retrieval.tokenizer import query_terms


_PAGE_LOCATION = re.compile(r"^\s*第\s*([1-9][0-9]{0,8})(?:\s*[-–—]\s*([1-9][0-9]{0,8}))?\s*页")
_MAX_PREVIEW_EDGE = 2400
# PDFium 的底层库不允许线程并发访问，包括同时打开不同文档。
_PDFIUM_LOCK = Lock()
_PAGE_QUERY_NOISE = {"扫码", "码器", "扫描", "二维", "维码", "设置", "图片", "原图", "手册", "文档",
                     "查看", "显示", "出来", "给我"}


def cited_pages(location: str) -> tuple[int, int] | None:
    match = _PAGE_LOCATION.match(location)
    if not match:
        return None
    first = int(match[1])
    last = int(match[2] or match[1])
    return (first, last) if last >= first else None


def page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    try:
        with _PDFIUM_LOCK, pdfium.PdfDocument(path) as document:
            return len(document)
    except (OSError, pdfium.PdfiumError):
        return 0


@lru_cache(maxsize=16)
def _page_texts(filename: str, size: int, modified: int) -> tuple[str, ...]:
    """按文件版本缓存定位文本；原图始终从原件渲染，不依赖提取嵌入图片。"""
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK, pdfium.PdfDocument(filename) as document:
        texts = []
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    texts.append(re.sub(r"\s+", "", textpage.get_text_range()).lower())
                finally:
                    textpage.close()
            finally:
                page.close()
        return tuple(texts)


def source_pages(path: Path, location: str, question: str = "", snippet: str = "") -> list[int]:
    """宽页码范围按原页文字定位；无法定位时只给原件入口，不伪造精确页码。"""
    cited = cited_pages(location)
    if cited and cited[1] - cited[0] < 3:
        # 通常索引已精确到页，避免每次问答重新读取整本手册。
        return list(range(cited[0], cited[1] + 1))
    if not question and not snippet:
        return []
    try:
        stat = path.stat()
        texts = _page_texts(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except (OSError, ValueError, RuntimeError):
        return []
    first, last = cited or (1, len(texts))
    numbers = range(max(1, first), min(last, len(texts)) + 1)
    terms = set(query_terms(question)) - _PAGE_QUERY_NOISE
    terms = {term for term in terms if len(term) > 1}
    excerpt_terms = {term for term in query_terms(snippet[:1600]) if len(term) > 1} - _PAGE_QUERY_NOISE
    candidates = [(number, texts[number - 1]) for number in numbers]
    if re.search(r"二维码|设置码|配置码|\bqr\b|barcode", question, re.I):
        code_pages = [(number, text) for number, text in candidates
                      if re.search(r"二维码|设置码|配置码|\bqr\b|barcode", text, re.I)]
        if code_pages:
            candidates = code_pages
    if not candidates:
        return []
    weights = {term: 1 + log((len(candidates) + 1) / (1 + sum(term in text for _, text in candidates)))
               for term in terms}
    ranked = []
    for number, text in candidates:
        score = sum(weight for term, weight in weights.items() if term in text)
        overlap = sum(term in text for term in excerpt_terms)
        if (terms and score == 0) or (not terms and overlap == 0):
            continue
        ranked.append((score, overlap / max(1, len(excerpt_terms)), number))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[2] for item in ranked[:3] if item[0] >= ranked[0][0] * .8] if ranked else []


def render_page(path: Path, page_number: int) -> bytes:
    import pypdfium2 as pdfium

    try:
        with _PDFIUM_LOCK, pdfium.PdfDocument(path) as document:
            if not 1 <= page_number <= len(document):
                raise IndexError(page_number)
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                if not all(isfinite(dimension) and dimension > 0 for dimension in (width, height)):
                    raise ValueError("PDF 页面尺寸无效")
                scale = min(4.0, _MAX_PREVIEW_EDGE / max(width, height))
                bitmap = page.render(scale=scale, draw_annots=True, limit_image_cache=True)
                try:
                    # Pillow 使用位图缓冲区，必须在关闭位图前完成 PNG 编码。
                    with bitmap.to_pil() as image, BytesIO() as output:
                        image.save(output, format="PNG")
                        return output.getvalue()
                finally:
                    bitmap.close()
            finally:
                page.close()
    except (OSError, pdfium.PdfiumError) as exc:
        raise ValueError("无法渲染此 PDF 页面") from exc
