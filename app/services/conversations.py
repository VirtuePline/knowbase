"""服务端问答上下文：绑定员工、权限版本及实际来源，不信任客户端历史正文。"""
from datetime import datetime, timedelta, timezone
import secrets

from app.services import access, database, mongo


def load(conversation_id: str, use_history: bool = True) -> list[dict]:
    actor = access.current_identity()
    if not actor or not conversation_id or not use_history:
        return []
    row = mongo.db().chat_sessions.find_one({"_id": conversation_id, "user": actor["username"],
        "stamp": access.permission_stamp(actor), "expire_at": {"$gt": datetime.now(timezone.utc)}})
    if not row:
        return []
    for doc_id in row.get("source_ids", []):
        doc = database.get(doc_id)
        if doc is None or not access.can_read(doc):
            return []
    return row.get("history", [])[-6:]


def save(conversation_id: str, question: str, response: dict) -> str:
    actor = access.current_identity()
    if not actor:
        return ""
    previous = load(conversation_id)
    source_ids = set()
    if previous:
        old = mongo.db().chat_sessions.find_one({"_id": conversation_id, "user": actor["username"]})
        source_ids.update((old or {}).get("source_ids", []))
    def collect(sources):
        for source in sources:
            if source.get("doc_id"):
                source_ids.add(source["doc_id"])
            collect(source.get("references", []))
    collect(response.get("sources", []))
    # 每次轮换ID；保存的历史仅由后端生成，下轮仍需重新检查当前权限版本。
    token = secrets.token_urlsafe(24)
    collection = mongo.db().chat_sessions
    collection.create_index("expire_at", expireAfterSeconds=0)
    collection.insert_one({"_id": token, "user": actor["username"], "stamp": access.permission_stamp(actor),
        "source_ids": list(source_ids),
        "history": (previous + [{"role": "user", "content": question},
                                {"role": "assistant", "content": response.get("answer", "")}])[-6:],
        "expire_at": datetime.now(timezone.utc) + timedelta(hours=2)})
    return token
