"""回答正文中的【记录 N】占位转换为可点击文档引用。"""
from types import SimpleNamespace

from app.core.answer.service import _link_kb_records


def _hit(doc_id: str, doc_name: str) -> SimpleNamespace:
    return SimpleNamespace(chunk=SimpleNamespace(doc_id=doc_id, doc_name=doc_name))


class TestRecordLink:
    def test_placeholder_becomes_clickable_reference(self):
        text = "【记录 1】\n摘录正文\n\n【记录 2】\n另一条"
        hits = [_hit("a1", "扫码枪扫不上"), _hit("a2", "音响没声音")]
        out = _link_kb_records(text, hits)
        assert "[扫码枪扫不上](doc://a1)" in out
        assert "[音响没声音](doc://a2)" in out
        assert "【记录" not in out

    def test_inline_reference_replaced(self):
        text = "处理步骤见【记录 1】第二步。"
        out = _link_kb_records(text, [_hit("a1", "贴标机闪退")])
        assert out == "处理步骤见[贴标机闪退](doc://a1)第二步。"

    def test_out_of_range_kept_as_is(self):
        text = "见【记录 5】。"
        assert _link_kb_records(text, [_hit("a1", "x")]) == text

    def test_no_placeholder_untouched(self):
        text = "普通回答，没有占位。"
        assert _link_kb_records(text, [_hit("a1", "x")]) == text

    def test_empty_name_fallback(self):
        out = _link_kb_records("【记录 1】", [_hit("a1", "  ")])
        assert "[未命名记录](doc://a1)" in out
