"""原始 PDF 页面通过与文档相同的权限提供。"""
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject
import pytest

from app import main
from app.api.routes import search_router
from app.domain.schemas import DocRecord
from app.services import access, database, kb, pdf_preview


@pytest.fixture
def pdf_documents(tmp_path: Path, monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=120, height=240)
    drawing_page = writer.add_blank_page(width=240, height=120)
    drawing = DecodedStreamObject()
    drawing.set_data(b"0 0 0 rg\n25 25 30 40 re f\n")
    drawing_page.replace_contents(drawing)
    source = tmp_path / "manual-original"
    with source.open("wb") as stream:
        writer.write(stream)
    public = DocRecord("manual", "读码器手册", source.name, ".pdf", source.stat().st_size,
                       "", 1, "", "ready", doc_type="doc", visibility="public")
    documents = {
        public.id: public,
        "private": replace(public, id="private", visibility="department"),
        "missing": replace(public, id="missing", stored_name="missing.pdf"),
    }
    monkeypatch.setattr(kb, "settings", replace(kb.settings, raw_dir=tmp_path))
    monkeypatch.setattr(database, "get", documents.get)
    monkeypatch.setattr(database, "get_attachments", lambda doc_id: [])
    monkeypatch.setattr(database, "department_names", lambda department_ids: {})
    monkeypatch.setattr(database, "recorder_label", lambda account, fallback_name: "")
    return source


@pytest.fixture
def public_client(monkeypatch):
    monkeypatch.setattr(main.auth, "public_identity", lambda token: None)
    app = main._make_app("PDF 预览测试", [search_router], "search")
    client = TestClient(app)
    yield client
    client.close()


def test_public_pdf_preview_serves_original_and_renders_textless_pages(pdf_documents, public_client):
    prefix = "/api/documents/manual"
    response = public_client.get(prefix + "/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert "no-store" in response.headers["cache-control"]
    assert response.content == pdf_documents.read_bytes()

    response = public_client.get(prefix + "/pages/2.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    with Image.open(BytesIO(response.content)) as page:
        assert page.size == (960, 480)
        assert page.getpixel((120, 340)) == (0, 0, 0)
        assert page.getpixel((0, 0)) == (255, 255, 255)

    preview = public_client.get(prefix + "/preview").json()
    assert preview["doc"]["id"] == "manual"
    assert "sections" in preview
    assert preview["pdf_page_count"] == 2
    assert public_client.get(prefix + "/pages/3.png").status_code == 404
    for doc_id in ("private", "missing"):
        for suffix in ("/pdf", "/pages/1.png"):
            assert public_client.get(f"/api/documents/{doc_id}{suffix}").status_code == 404


def test_pdf_sources_preserve_cited_pages_and_permission(pdf_documents):
    sources = [
        {"doc_id": "manual", "location": "第2页 · 结构段3", "references": [
            {"doc_id": "manual", "location": "第1-2页 第2段"},
            {"doc_id": "private", "location": "第1页"},
        ]},
        {"doc_id": "manual", "location": "第1-10页"},
        {"doc_id": "missing", "location": "第2页"},
    ]
    with access.identity_scope({"public_only": True}):
        kb.add_pdf_source_previews(sources)

    assert sources[0]["pdf_pages"] == [2]
    assert sources[0]["pdf_page"] == 2
    assert sources[0]["references"][0]["pdf_pages"] == [1, 2]
    assert "pdf_available" not in sources[0]["references"][1]
    assert sources[1]["pdf_available"] is True
    assert sources[1]["pdf_page"] == 1
    assert "pdf_pages" not in sources[1]
    assert "pdf_available" not in sources[2]


def test_only_authorized_answer_evidence_is_used_to_locate_pdf_images(pdf_documents, monkeypatch):
    calls = []
    def locate(*args):
        calls.append(args)
        return [2]
    monkeypatch.setattr(pdf_preview, "source_pages", locate)
    sources = [{"doc_id": "manual", "location": "第1-10页"},
               {"doc_id": "private", "location": "第1-10页"},
               {"doc_id": "manual", "scoring_method": "search_context"}]
    with access.identity_scope({"public_only": True}):
        kb.add_pdf_source_previews(sources, "factory reset barcode")
    assert sources[0]["pdf_pages"] == [2]
    assert "pdf_available" not in sources[1]
    assert sources[2]["pdf_available"] is True
    assert "pdf_pages" not in sources[2]
    assert len(calls) == 1


def test_missing_pdfium_degrades_gracefully(pdf_documents, public_client, monkeypatch):
    """离线镜像未安装 pypdfium2 时：问答来源照常返回，页数/页码/渲染降级，绝不 500。"""
    monkeypatch.setattr(pdf_preview, "pdfium", None)
    sources = [{"doc_id": "manual", "location": "第1-2页"}]
    with access.identity_scope({"public_only": True}):
        kb.add_pdf_source_previews(sources, "factory reset barcode")
    assert sources[0]["pdf_available"] is True        # 原件仍可打开
    assert sources[0]["pdf_page"] == 1                # 引用页码仍按索引保留
    assert "pdf_pages" not in sources[0]              # 不做逐页定位
    assert pdf_preview.page_count(pdf_documents) == 0
    assert pdf_preview.source_pages(pdf_documents, "", "q") == []
    with pytest.raises(pdf_preview.PDFPreviewUnavailable):
        pdf_preview.render_page(pdf_documents, 1)
    response = public_client.get("/api/documents/manual/pages/1.png")
    assert response.status_code == 501
