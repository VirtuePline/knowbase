"""文档上传 / 预览 / 下载 / 替换 / Markdown 编辑 / 列表 / 删除 / 重建。

管理操作只走管理端接口，不通过对话执行，查询端也不挂载这些接口。
"""
from __future__ import annotations

import datetime
import hashlib
import re
import uuid
from collections import Counter
from dataclasses import replace as replace_record
from pathlib import Path

from app.config import settings
from app.core.ingest.pipeline import Ingester, sha256_of
from app.domain.schemas import Chunk, DocRecord
from app.domain.taxonomy import (
    DOC_CATEGORY_ORDER,
    DOC_OTHER,
    classify_document,
    normalize_doc_categories,
    normalize_doc_category,
    record_categories,
)
from app.services import database, recordmeta
from app.services.preview import is_editable, merge_preview
from app.services.retrieval_service import retrieval

_SAFE_NAME = re.compile(r"[^\w一-鿿.\-]+")
_ENCODINGS = ("utf-8-sig", "gb18030", "utf-8")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# 录入时可选的解决状态（时间线徽标按此显示；时间线上的人工标记优先级更高）
RESOLVED_VALUES = ("resolved", "improving", "unresolved")

# 上传多分类标签的分隔符（前端「|」连接，容错逗号）
_CAT_SPLIT = re.compile(r"[|,，、]")


def _norm_resolved(value: str) -> str:
    text = (value or "").strip().lower()
    return text if text in RESOLVED_VALUES else ""


class DocumentNotFound(Exception):
    pass


def _safe_stored_name(original: str, doc_id: str) -> str:
    suffix = Path(original).suffix.lower()
    stem = _SAFE_NAME.sub("_", Path(original).stem)[:60] or "doc"
    return f"{doc_id}_{stem}{suffix}"


def _decode(raw: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _require(doc_id: str) -> DocRecord:
    record = database.get(doc_id)
    if not record:
        raise DocumentNotFound(doc_id)
    return record


def _tracking_root(parent_id: str, doc_id: str) -> str:
    """关系只在创建时设定，父记录必须是已入库的运维记录。"""
    if not parent_id:
        return doc_id
    if parent_id == doc_id:
        raise ValueError("记录不能跟踪自身")
    parent = database.get(parent_id)
    if not parent:
        raise ValueError("所跟踪的记录不存在")
    is_record = parent.doc_type == "record" or (
        not parent.doc_type and "常识库" not in record_categories(parent))
    if parent.status != "ready" or not is_record:
        raise ValueError("只能跟踪已完成入库的运维问题记录")
    return parent.root_id or parent.id


def _raw_path(record: DocRecord) -> Path:
    return settings.raw_dir / record.stored_name


def _doc_dict(record: DocRecord) -> dict:
    data = record.to_dict()
    data["editable"] = is_editable(record.ext)
    data["has_source"] = _raw_path(record).is_file()
    return data


def _check_upload(filename: str, content: bytes) -> tuple[str, str]:
    if not filename:
        raise ValueError("文件名为空")
    original = Path(filename).name
    ext = Path(original).suffix.lower()
    if ext not in settings.supported_exts:
        hint = {".doc": "请另存为 .docx", ".xls": "请另存为 .xlsx"}.get(ext, "")
        raise ValueError(f"暂不支持 {ext or '无扩展名'} 格式" + (f"，{hint}" if hint else ""))
    if not content:
        raise ValueError("文件内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 100MB，请拆分后再上传")
    return original, ext


def _ingest(record: DocRecord, path: Path, category: str = "") -> DocRecord:
    """解析 + 分析 + 切块，并把结果写回登记表。成功后重建索引。

    category 非空时作为强制分类进入分块（覆盖自动判定），保证检索层按分类生效。
    """
    result = Ingester().ingest(path, record.id, record.name, record.ext.lstrip("."),
                               category=category)
    if not result.ok:
        database.update_status(record.id, 0, "failed", result.error)
    else:
        database.update_status(record.id, result.chunks, "ready",
                               summary=result.summary, skipped=result.skipped,
                               category=result.category)
        retrieval.rebuild()
    return database.get(record.id)


def _with_categories(docs: list[DocRecord]) -> list[DocRecord]:
    """为无分类的旧文档补算展示值；查询接口不修改数据库。"""
    ingester = Ingester()
    filled: list[DocRecord] = []
    for doc in docs:
        if doc.category:
            filled.append(doc)
            continue
        chunks = ingester.read_chunks(doc.id) if doc.status == "ready" else []
        category = classify_document(
            doc.name,
            [chunk.heading for chunk in chunks if chunk.heading],
            [chunk.summary for chunk in chunks if chunk.summary],
            doc.summary,
        )
        filled.append(replace_record(doc, category=category))
    return filled


def list_documents(category: str = "", doc_type: str = "") -> list[dict]:
    """文档列表。doc_type=doc 返回基础文档（含常识库：文档即常识库，不再分开），record 只返回运维问题记录。"""
    docs = _with_categories(database.list_all())
    if doc_type == "doc":
        docs = [doc for doc in docs if doc.doc_type == "doc"]
    elif doc_type == "record":
        docs = [doc for doc in docs if doc.doc_type == "record"]
    if category:
        name = normalize_doc_category(category)
        docs = [doc for doc in docs if name in record_categories(doc)]
    return [_doc_dict(doc) for doc in docs]


def category_counts(doc_type: str = "") -> list[dict]:
    """各分类的文档数，顺序固定，供前端筛选条使用；统计范围与列表过滤一致。"""
    docs = _with_categories(database.list_all())
    if doc_type == "doc":
        docs = [doc for doc in docs if doc.doc_type == "doc"]
    elif doc_type == "record":
        docs = [doc for doc in docs if doc.doc_type == "record"]
    counter = Counter(tag for doc in docs for tag in record_categories(doc))   # 多标签逐个计数
    return [{"name": name, "count": counter.get(name, 0)} for name in DOC_CATEGORY_ORDER]


def set_category(doc_id: str, category: str) -> dict:
    """人工修正分类：重置为单标签。分类只落在登记表，不影响分块与索引。"""
    return set_categories(doc_id, [category] if (category or "").strip() else [])


def set_categories(doc_id: str, categories: list) -> dict:
    """人工设置全部分类标签（可多选）：首标签为主分类，空列表清空标签。"""
    _require(doc_id)
    tags: list[str] = []
    for value in categories or []:
        text = (value or "").strip()
        if text and text not in DOC_CATEGORY_ORDER:
            raise ValueError(f"未知分类：{text}，可选值：{'、'.join(DOC_CATEGORY_ORDER)}")
        if text and text not in tags:
            tags.append(normalize_doc_category(text))
    database.set_categories(doc_id, tags)
    return _doc_dict(_require(doc_id))


def set_vendor(doc_id: str, vendor: str) -> dict:
    """人工设置 / 清除记录的厂商（厂商系统类记录按厂商归类）。"""
    _require(doc_id)
    value = (vendor or "").strip()
    if value and value not in database.list_vendors():
        raise ValueError(f"未知厂商：{value}，请先在「模板与厂商设置」里添加")
    database.set_vendor(doc_id, value)
    return _doc_dict(_require(doc_id))


def set_document_time(doc_id: str, date: str) -> dict:
    """人工修正记录时间（年月日）；时分秒保留原值，仅日期部分替换。"""
    record = _require(doc_id)
    text = (date or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("时间格式应为 YYYY-MM-DD，例如 2026-11-04")
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"无效日期：{text}") from None
    rest = record.uploaded_at[10:]
    rest = rest if re.fullmatch(r"\s\d{2}:\d{2}:\d{2}", rest) else " 00:00:00"
    database.set_uploaded_at(doc_id, text + rest)
    return _doc_dict(_require(doc_id))


def upload(filename: str, content: bytes, category: str = "", doc_type: str = "doc",
           date: str = "", vendor: str = "", resolved: str = "", *,
           categories: str = "", uploaded_by: str = "", uploaded_by_name: str = "",
           parent_id: str = "") -> dict:
    """上传文档；category / categories 非空时入库后强制设置分类，优先于自动判定。

    doc_type：doc=基础文档（文档管理页上传），record=运维问题记录（运维问题上传页上传）。
    categories 为多标签（| 或逗号分隔），非空时按其设置全部分类，首标签为主分类；
    category 为单值兜底（常识注入等固定分类场景）。
    date 为记录时间日期部分（YYYY-MM-DD），运维问题上传时人工选择的问题发生日期。
    vendor 为厂商系统类记录的厂商，入库后落登记表。
    resolved 为录入时选择的解决状态（resolved / improving / unresolved），时间线按此显示。
    uploaded_by 为创建账号；uploaded_by_name 为运维人用户名（默认当前登录用户名，可改）；
    parent_id 非空时新建独立追踪，不覆盖或复用父记录。
    """
    original, ext = _check_upload(filename, content)
    norm_resolved = _norm_resolved(resolved)
    cats = normalize_doc_categories(part for part in _CAT_SPLIT.split(categories or "") if part.strip())
    if not cats and (category or "").strip():
        cats = [normalize_doc_category(category)]
    primary = cats[0] if cats else ""

    doc_id = uuid.uuid4().hex[:12]
    uploaded_by = (uploaded_by or "").strip()
    uploaded_by_name = (uploaded_by_name or "").strip()
    parent_id = (parent_id or "").strip()
    root_id = _tracking_root(parent_id, doc_id)
    stored = _safe_stored_name(original, doc_id)
    target = settings.raw_dir / stored
    target.write_bytes(content)

    digest = sha256_of(target)
    existing = None if parent_id else database.find_by_hash(
        digest, uploaded_by=uploaded_by, parent_id="")
    if existing and existing.status == "ready":
        target.unlink(missing_ok=True)
        if cats:
            database.set_categories(existing.id, cats)
        if vendor:
            database.set_vendor(existing.id, vendor)
        if not date and existing.doc_type == "record":
            date = datetime.date.today().isoformat()   # 运维记录未指定日期时默认当前日期
        if date:
            set_document_time(existing.id, date)   # 重复文件也按本次选择修正记录时间
        if norm_resolved:
            database.set_resolved(existing.id, norm_resolved)   # 重复上传时按本次选择更新解决状态
        existing = _require(existing.id)
        return {"duplicated": True, "doc": _doc_dict(existing)}

    record = DocRecord(
        id=doc_id, name=original, stored_name=stored, ext=ext, size=len(content),
        sha256=digest, chunks=0, uploaded_at=database.now_iso(), status="processing",
        doc_type="record" if parent_id else (doc_type if doc_type in {"doc", "record"} else "doc"),
        resolved=norm_resolved,
        uploaded_by=uploaded_by, uploaded_by_name=uploaded_by_name,
        parent_id=parent_id, root_id=root_id,
    )
    database.add(record)
    result = _ingest(record, target, category=primary)
    if cats:
        database.set_categories(result.id, cats)
        result = _require(doc_id)
    if vendor:
        database.set_vendor(doc_id, vendor)
        result = _require(doc_id)
    if not date and record.doc_type == "record":
        date = datetime.date.today().isoformat()   # 运维记录未指定日期时默认当前日期
    if date:
        set_document_time(doc_id, date)
        result = _require(doc_id)
    return {"duplicated": False, "doc": _doc_dict(result)}


def replace_file(doc_id: str, filename: str, content: bytes) -> dict:
    """用新文件替换已有文档：保留文档 ID 与首次上传时间，重新解析、分析并重建索引。

    新文件解析失败时不动原文档，原文件、分块与检索结果保持不变。
    """
    record = _require(doc_id)
    original, ext = _check_upload(filename, content)

    stored = _safe_stored_name(original, doc_id)
    staging = settings.raw_dir / f".replace_{uuid.uuid4().hex[:8]}{ext}"
    staging.write_bytes(content)
    try:
        digest = sha256_of(staging)
        if digest == record.sha256:
            raise ValueError("新文件与当前文档内容完全相同，无需替换")
        duplicate = None if record.parent_id else database.find_by_hash(
            digest, uploaded_by=record.uploaded_by, parent_id="")
        if duplicate and duplicate.status == "ready":
            if duplicate.id == doc_id:
                raise ValueError("新文件与当前文档内容完全相同，无需替换")
            raise ValueError(f"新文件与已有文档《{duplicate.name}》内容完全相同，未替换")

        # 先在临时 ID 下试解析，确认新文件可用再替换，避免把可用文档换成坏文件
        probe_id = f"probe{uuid.uuid4().hex[:8]}"
        ingester = Ingester()
        probe = ingester.ingest(staging, probe_id, original, ext.lstrip("."))
        ingester.remove(probe_id)
        if not probe.ok:
            raise ValueError(f"新文件无法入库，原文档未改动：{probe.error}")

        target = settings.raw_dir / stored
        old_path = _raw_path(record)
        staging.replace(target)
        if old_path != target:
            old_path.unlink(missing_ok=True)
    finally:
        staging.unlink(missing_ok=True)

    updated = replace_record(
        record, name=original, stored_name=stored, ext=ext, size=len(content),
        sha256=digest, status="processing",
    )
    database.add(updated)
    return {"replaced": True, "doc": _doc_dict(_ingest(updated, target, category=record.category))}


def delete(doc_id: str) -> bool:
    record = database.get(doc_id)
    if not record:
        return False
    if database.count_children(doc_id):
        raise ValueError("该记录存在后续追踪，请先删除子追踪记录")
    _raw_path(record).unlink(missing_ok=True)
    Ingester().remove(doc_id)
    database.delete(doc_id)
    retrieval.rebuild()
    return True


def get_document(doc_id: str) -> dict:
    return _doc_dict(_require(doc_id))


def preview(doc_id: str) -> dict:
    record = _require(doc_id)
    chunks = Ingester().read_chunks(doc_id)
    return {"doc": _doc_dict(record), "sections": merge_preview(chunks)}


def source_file(doc_id: str) -> tuple[Path, str]:
    """原始文件路径与下载文件名。"""
    record = _require(doc_id)
    path = _raw_path(record)
    if not path.is_file():
        raise ValueError("原始文件未保存在知识库中（早期命令行导入的文档），请用「替换」重新上传原件")
    return path, f"{record.name}{record.ext}"   # 下载用原名带扩展名


def _restore_from_chunks(chunks: list[Chunk]) -> str:
    """原始文件缺失时，用分块里保存的章节原文拼回 Markdown，至少让编辑器能看到内容。"""
    parts: list[str] = []
    last_heading = None
    for chunk in chunks:
        if chunk.heading and chunk.heading != last_heading:
            parts.append(f"## {chunk.heading}")
            last_heading = chunk.heading
        parts.append(chunk.text.strip())
    return "\n\n".join(p for p in parts if p) + "\n"


def read_source(doc_id: str) -> dict:
    """读取原始文本内容，供在线编辑。仅 md / txt 可编辑。"""
    record = _require(doc_id)
    if not is_editable(record.ext):
        raise ValueError(f"{record.ext} 文件不支持在线编辑，仅支持 .md / .txt；可下载后修改，再用「替换」上传")

    path = _raw_path(record)
    if path.is_file():
        return {"id": record.id, "name": record.name, "content": _decode(path.read_bytes()), "restored": False}

    chunks = Ingester().read_chunks(doc_id)
    if not chunks:
        raise ValueError("原始文件与分块均已丢失，请重新上传")
    return {"id": record.id, "name": record.name, "content": _restore_from_chunks(chunks), "restored": True}


def save_markdown(content: str, doc_id: str = "", filename: str = "",
                  category: str = "", vendor: str = "", resolved: str = "", *,
                  categories: str = "", uploaded_by: str = "", uploaded_by_name: str = "",
                  parent_id: str = "") -> dict:
    """新建或更新 Markdown / 文本记录，并立即重建索引。

    doc_id 为空时新建；传入已有 doc_id 时覆盖原文件与分块。
    categories 为多标签（| 或逗号分隔），非空时按其落库（首标签为主分类），覆盖自动判定；
    category 为单值兜底。两者都为空时保留自动判定结果。
    vendor 仅对挂有「厂商系统」标签的记录有意义：其余分类忽略。
    resolved 为录入时选择的解决状态；空串时保留原状态（新建则为未标记）。
    uploaded_by / uploaded_by_name / parent_id 仅用于创建，编辑保留原始创建账号、运维人与跟踪关系。
    """
    if not content.strip():
        raise ValueError("内容为空，未保存")
    if vendor and vendor not in recordmeta.list_vendors():
        raise ValueError("未知厂商，请先在「模板与厂商设置」里添加")
    cats = normalize_doc_categories(part for part in _CAT_SPLIT.split(categories or "") if part.strip())
    if not cats and (category or "").strip():
        cats = [normalize_doc_category(category)]
    primary = cats[0] if cats else ""

    if doc_id:
        if parent_id:
            raise ValueError("跟踪关系创建后不可修改，请新建追踪记录")
        record = _require(doc_id)
        if not is_editable(record.ext):
            raise ValueError(f"{record.ext} 文件不支持在线编辑，仅支持 .md / .txt")
        raw = (filename or record.name).strip() or record.name
        # 记录名称统一不带后缀（默认保存为 .md；原为 .txt 的仍保持 .txt）
        ext = ".txt" if (Path(raw).suffix.lower() == ".txt" or record.ext == ".txt") else ".md"
        name = Path(raw).stem.strip() or record.name
    else:
        date_suffix = database.now_iso()[:10].replace("-", "")
        base = Path((filename or "").strip() or f"运维记录_{date_suffix}").name
        ext = ".txt" if base.lower().endswith(".txt") else ".md"
        name = base[: -len(ext)] if base.lower().endswith(ext) else base
        name = name.strip() or f"运维记录_{date_suffix}"
        doc_id = uuid.uuid4().hex[:12]
        record = None

    if record:
        uploaded_by, parent_id, root_id = record.uploaded_by, record.parent_id, record.root_id
        uploaded_by_name = record.uploaded_by_name   # 编辑保留原运维人
    else:
        uploaded_by = (uploaded_by or "").strip()
        uploaded_by_name = (uploaded_by_name or "").strip()
        parent_id = (parent_id or "").strip()
        root_id = _tracking_root(parent_id, doc_id)

    if not name:
        raise ValueError("文件名不能为空")

    payload = content.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    duplicate = None if parent_id else database.find_by_hash(
        digest, uploaded_by=uploaded_by, parent_id="")
    if duplicate and duplicate.id != doc_id and duplicate.status == "ready":
        return {"duplicated": True, "doc": _doc_dict(duplicate)}

    stored = _safe_stored_name(f"{name}{ext}", doc_id)
    target = settings.raw_dir / stored
    target.write_bytes(payload)

    if record and record.stored_name != stored:
        _raw_path(record).unlink(missing_ok=True)

    if record:
        new_record = replace_record(
            record, name=name, stored_name=stored, ext=ext, size=len(payload),
            sha256=digest, status="processing", resolved=_norm_resolved(resolved) or record.resolved,
        )
    else:
        new_record = DocRecord(
            id=doc_id, name=name, stored_name=stored, ext=ext, size=len(payload),
            sha256=digest, chunks=0, uploaded_at=database.now_iso(), status="processing",
            doc_type="record", resolved=_norm_resolved(resolved),
            uploaded_by=uploaded_by, uploaded_by_name=uploaded_by_name,
            parent_id=parent_id, root_id=root_id,
        )
    database.add(new_record)
    doc = _ingest(new_record, target, category=primary or (record.category if record else ""))
    if cats:   # 人工选择的分类标签优先于自动判定
        database.set_categories(doc.id, cats)
        doc = _require(doc.id)
    elif category:
        normalized = normalize_doc_category(category)
        if doc.category != normalized:
            database.set_categories(doc.id, [normalized])
            doc = _require(doc.id)
    if "厂商系统" not in record_categories(doc):
        vendor = ""   # 厂商只对挂有「厂商系统」标签的记录有意义，换分类时清空避免残留
    if vendor and doc.vendor != vendor:
        database.set_vendor(doc.id, vendor)
        doc = replace_record(doc, vendor=vendor)
    return {"duplicated": False, "created": record is None, "doc": _doc_dict(doc)}


def reingest(doc_id: str) -> DocRecord:
    """按当前分析规则重新解析一份文档（原始文件需仍在）。"""
    record = _require(doc_id)
    path = _raw_path(record)
    if not path.is_file():
        raise ValueError("原始文件缺失，无法重新分析")
    return _ingest(record, path)


def rebuild() -> dict:
    index = retrieval.rebuild()
    return {"chunks": len(index.chunks), "documents": index.doc_count()}


def stats() -> dict:
    return retrieval.stats()
