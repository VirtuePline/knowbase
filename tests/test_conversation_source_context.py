"""产品上下文由服务端来源恢复，联网失败后仍可续问；不访问数据库。"""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import access, conversations


def test_search_context_survives_web_reply_and_uses_current_authorized_document_name(monkeypatch):
    rows = {}
    collection = MagicMock()
    collection.find_one.side_effect = lambda query: copy.deepcopy(rows.get(query["_id"]))
    collection.insert_one.side_effect = lambda row: rows.update({row["_id"]: copy.deepcopy(row)})
    document = SimpleNamespace(id="scanner", name="海康机器人无线手持读码器用户手册.pdf")
    monkeypatch.setattr(conversations.mongo, "db", lambda: SimpleNamespace(chat_sessions=collection))
    monkeypatch.setattr(conversations.database, "get", lambda key: document if key == document.id else None)
    monkeypatch.setattr(access, "permission_stamp", lambda actor: "current-permissions")
    readable = {"scanner"}
    monkeypatch.setattr(access, "can_read", lambda doc: doc.id in readable)

    with access.identity_scope({"username": "engineer"}):
        token = conversations.save("", "什么是快速模式", {
            "answer": "联网参考。", "sources": [
                {"doc_id": "", "kind": "web", "scoring_method": "web"},
                {"doc_id": "scanner", "doc_name": "不应信任保存时的显示名",
                 "scoring_method": "search_context"},
            ],
        })
        history = conversations.load(token)
        assert history[-1]["source_docs"] == [{"doc_id": "scanner", "doc_name": document.name}]
        token = conversations.save(token, "如何设置", {
            "answer": "联网搜索暂时失败。", "sources": [
                {"doc_id": "scanner", "scoring_method": "search_context"},
            ],
        })
        assert conversations.load(token)[-1]["source_docs"] == history[-1]["source_docs"]
        readable.clear()
        assert conversations.load(token) == []
