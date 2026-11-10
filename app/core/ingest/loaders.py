"""文档解析：PDF / DOCX / XLSX / CSV / TXT / MD → Block 列表。

解析失败或无有效文本时抛出 LoaderError，由入库流水线记录为失败原因。
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time
from pathlib import Path

from app.domain.schemas import Block

_ENCODINGS = ("utf-8-sig", "gb18030")
_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_PAGE_OUTLINE_TITLE = re.compile(r"^(?:第\s*\d+\s*页|page\s*\d+|p\.?\s*\d+|\d+)$", re.IGNORECASE)


class LoaderError(Exception):
    pass


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M") if (value.hour or value.minute) else value.strftime("%Y-%m-%d")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _clean_table(rows: list[list[str]]) -> list[list[str]]:
    """去掉全空行与右侧全空列，并把每行补齐到相同列数。"""
    rows = [[c.replace("\r", "").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(max((i + 1 for i, c in enumerate(r) if c), default=0) for r in rows)
    return [(r + [""] * width)[:width] for r in rows]


def _load_markdown(path: Path) -> list[Block]:
    """按标题切节：每个标题下的正文是一条独立记录，不与其他节合并。"""
    blocks: list[Block] = []
    heading = ""
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            blocks.append(Block(text=body, heading=heading, location=f"章节「{heading}」" if heading else ""))

    for line in _read_text(path).splitlines():
        match = _MD_HEADING.match(line)
        if match:
            flush()
            heading, lines = match.group(2).strip(), []
        else:
            lines.append(line)
    flush()
    return blocks


def _load_txt(path: Path) -> list[Block]:
    text = _read_text(path).strip()
    return [Block(text=text)] if text else []


def _load_csv(path: Path) -> list[Block]:
    text = _read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = _clean_table([list(r) for r in csv.reader(io.StringIO(text), dialect)])
    if not rows:
        return []
    return [Block(text="", table=rows, heading=path.stem, location="")]


def _load_xlsx(path: Path) -> list[Block]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # 加密、损坏或伪装扩展名
        raise LoaderError(f"Excel 无法打开：{type(exc).__name__}") from exc

    blocks: list[Block] = []
    try:
        for sheet in workbook.worksheets:
            rows = _clean_table([[_cell_text(v) for v in row] for row in sheet.iter_rows(values_only=True)])
            if rows:
                blocks.append(Block(text="", table=rows, heading=sheet.title, location=f"工作表「{sheet.title}」"))
    finally:
        workbook.close()
    return blocks


def _load_docx(path: Path) -> list[Block]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(str(path))
    except Exception as exc:
        raise LoaderError(f"Word 无法打开：{type(exc).__name__}") from exc

    blocks: list[Block] = []
    heading = ""
    lines: list[str] = []
    table_no = 0

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            blocks.append(Block(text=body, heading=heading, location=f"章节「{heading}」" if heading else ""))
        lines.clear()

    # 按正文顺序遍历段落与表格，保证表格挂在正确的章节下
    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            if text and (style.lower().startswith("heading") or style.startswith("标题") or style == "Title"):
                flush()
                heading = text
            elif text:
                lines.append(text)
        elif tag == "tbl":
            flush()
            table_no += 1
            table = Table(element, document)
            rows = _clean_table([[cell.text for cell in row.cells] for row in table.rows])
            if rows:
                blocks.append(Block(text="", table=rows, heading=heading or f"表格{table_no}",
                                    location=f"表格{table_no}"))
    flush()
    return blocks


def _pdf_outline_titles(reader) -> dict[int, str]:
    """读取 PDF 书签对应的起始页，作为优先于正文猜测的章节结构。"""
    titles: dict[int, str] = {}

    def walk(items, parent: str = "") -> None:
        previous = parent
        for item in items:
            if isinstance(item, list):
                # 嵌套列表是上一书签的子层级，用父标题组成结构路径
                walk(item, previous)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            try:
                page_index = reader.get_destination_page_number(item)
            except Exception:
                continue
            if page_index is None or page_index < 0 or not title or _PAGE_OUTLINE_TITLE.match(title):
                continue
            path = f"{parent} / {title}" if parent else title
            titles.setdefault(page_index, path)
            previous = path

    try:
        walk(reader.outline)
    except Exception:
        return {}
    return titles


def _load_pdf(path: Path) -> list[Block]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise LoaderError(f"PDF 无法打开：{type(exc).__name__}") from exc
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise LoaderError("PDF 已加密，请先解除密码保护") from exc

    # PDF 页面只是文档载体，不直接作为分析结构；有书签时保留章节标题供分析器归并，
    # 没有书签时再由分析器根据正文标题和非页面边界重新分段。
    outline_titles = _pdf_outline_titles(reader)
    blocks: list[Block] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if text:
            blocks.append(Block(
                text=text,
                location=f"第{number}页",
                heading=outline_titles.get(number - 1, ""),
            ))
    if not blocks and reader.pages:
        raise LoaderError("PDF 未提取到文本，可能是扫描件，请先 OCR 后再上传")
    return blocks


_LOADERS = {
    ".md": _load_markdown,
    ".txt": _load_txt,
    ".csv": _load_csv,
    ".xlsx": _load_xlsx,
    ".xlsm": _load_xlsx,
    ".docx": _load_docx,
    ".pdf": _load_pdf,
}


def load_blocks(path: Path) -> list[Block]:
    ext = path.suffix.lower()
    loader = _LOADERS.get(ext)
    if loader is None:
        hint = {".doc": "，请另存为 .docx", ".xls": "，请另存为 .xlsx"}.get(ext, "")
        raise LoaderError(f"暂不支持 {ext or '无扩展名'} 格式{hint}")
    blocks = [b for b in loader(path) if b.text.strip() or b.table]
    if not blocks:
        raise LoaderError("文件中没有可识别的文本内容")
    return blocks
