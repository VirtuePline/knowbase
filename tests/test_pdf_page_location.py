"""跨页来源仍须定位具体图文页，原件替换后不能复用旧文本。"""
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.services import pdf_preview


def write_manual(path: Path, target: int = 5):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for number in range(1, 7):
        page = writer.add_blank_page(width=240, height=160)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)})})
        text = "Factory reset barcode" if number == target else "Factory reset by switch" if number == 2 else "Device setup"
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 20 110 Td ({text}) Tj ET\n0 0 0 rg 20 20 20 20 re f".encode())
        page.replace_contents(content)
    with path.open("wb") as stream:
        writer.write(stream)


def test_wide_or_unknown_locations_choose_the_actual_setting_code_page(tmp_path):
    path = tmp_path / "manual.pdf"
    write_manual(path)
    for location in ("第1-6页 · 结构段2", ""):
        assert pdf_preview.source_pages(path, location, "factory reset barcode") == [5]
    assert pdf_preview.source_pages(path, "第1-4页", "factory reset") == [2]
    assert pdf_preview.source_pages(path, "第1-6页", "unrelated term") == []


def test_replacing_pdf_invalidates_cached_page_text(tmp_path):
    path = tmp_path / "manual.pdf"
    write_manual(path)
    assert pdf_preview.source_pages(path, "", "factory reset barcode") == [5]
    write_manual(path, target=6)
    assert pdf_preview.source_pages(path, "", "factory reset barcode") == [6]


def test_parallel_requests_render_complete_pages(tmp_path):
    path = tmp_path / "manual.pdf"
    write_manual(path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(lambda number: pdf_preview.render_page(path, number), (5, 2, 6)))
    for raw in pages:
        with Image.open(BytesIO(raw)) as image:
            assert image.size == (960, 640)
            assert image.getpixel((100, 500)) == (0, 0, 0)
