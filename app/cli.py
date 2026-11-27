"""命令行：python -m app.cli <command>"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.config import settings
from app.core.ingest.pipeline import Ingester, sha256_of
from app.domain.schemas import DocRecord, TOOL_FILE_EXTS
from app.services import database
from app.services import kb
from app.services.retrieval_service import retrieval


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    host = args.host or settings.host
    port = args.port or settings.port
    # KB_RELOAD=1 时监听代码文件变化并自动重启（配合整项目挂载：改宿主机代码容器即时生效）。
    reload_on = os.environ.get("KB_RELOAD", "").strip() == "1"
    print(f"知识库：http://{host}:{port}（可免登录查询公共资料，登录后使用账号权限）")
    uvicorn.run("app.main:app", host=host, port=port, log_level="info", reload=reload_on)


def cmd_ingest(args: argparse.Namespace) -> None:
    import shutil
    import uuid

    database.init_db()
    target = Path(args.path)
    files: list[Path] = []
    if target.is_dir():
        supported = settings.supported_exts | TOOL_FILE_EXTS
        files = [p for p in sorted(target.rglob("*")) if p.is_file() and p.suffix.lower() in supported]
    elif target.is_file():
        files = [target]
    if not files:
        print(f"未找到可入库的文件：{target}")
        sys.exit(1)

    ingester = Ingester()
    ok = fail = 0
    has_indexable_files = False
    for path in files:
        if kb.is_tool_file(path.name):
            if path.stat().st_size > kb.MAX_TOOL_UPLOAD_BYTES:
                print(f"[失败] {path.name} -> {kb.TOOL_UPLOAD_LIMIT_MESSAGE}")
                fail += 1
                continue
            try:
                with path.open("rb") as content:
                    result = kb.upload_tool(path.name, content)
            except (OSError, ValueError) as exc:
                print(f"[失败] {path.name} -> {exc}")
                fail += 1
                continue
            if result["duplicated"]:
                print(f"[跳过] 已存在：{path.name}")
            else:
                print(f"[成功] {path.name} -> 工具文件，仅供下载")
                ok += 1
            continue

        has_indexable_files = True
        digest = sha256_of(path)
        existing = database.find_by_hash(digest, indexable=True)
        if existing and existing.status == "ready":
            stored = settings.raw_dir / existing.stored_name
            if not stored.is_file():
                # 早期版本命令行导入时没有保存原件，这里顺带补存，便于下载与在线编辑
                fixed = kb._safe_stored_name(existing.name, existing.id)
                shutil.copy2(path, settings.raw_dir / fixed)
                database.set_stored_name(existing.id, fixed)
                print(f"[修复] 已补存原始文件：{path.name}")
            else:
                print(f"[跳过] 已存在：{path.name}")
            continue

        doc_id = uuid.uuid4().hex[:12]
        name = path.name
        stored_name = kb._safe_stored_name(name, doc_id)
        stored = settings.raw_dir / stored_name
        shutil.copy2(path, stored)     # 原件存入知识库，供下载、编辑与重新分析
        record = DocRecord(
            id=doc_id, name=name, stored_name=stored_name, ext=path.suffix.lower(),
            size=path.stat().st_size, sha256=digest, chunks=0,
            uploaded_at=database.now_iso(), status="processing",
            doc_type="doc",
        )
        database.add(record)
        result = ingester.ingest(stored, doc_id, name, path.suffix.lower().lstrip("."), content_type="doc")
        if result.ok:
            database.update_status(doc_id, result.chunks, "ready",
                                   summary=result.summary, skipped=result.skipped)
            skipped = f"，忽略 {result.skipped} 个非主要部分" if result.skipped else ""
            print(f"[成功] {name} -> {result.chunks} 个检索单元{skipped}")
            ok += 1
        else:
            database.update_status(doc_id, 0, "failed", result.error)
            print(f"[失败] {name} -> {result.error}")
            fail += 1

    if has_indexable_files:
        index = retrieval.rebuild()
        print(f"入库完成：成功 {ok}，失败 {fail}；索引共 {len(index.chunks)} 个分块 / {index.doc_count()} 份文档")
    else:
        print(f"导入完成：成功 {ok}，失败 {fail}")


def cmd_reanalyze(args: argparse.Namespace) -> None:
    """按新的分析规则重新处理已入库文档：重新判定主要内容、生成摘要。"""
    database.init_db()
    records = [database.get(args.doc_id)] if args.doc_id else database.list_all()
    records = [r for r in records if r]
    if not records:
        print("没有可处理的文档")
        sys.exit(1)
    for record in records:
        try:
            updated = kb.reingest(record.id)
        except ValueError as exc:
            print(f"[跳过] {record.name} -> {exc}")
            continue
        if updated.status == "ready":
            skipped = f"，忽略 {updated.skipped} 个非主要部分" if updated.skipped else ""
            print(f"[完成] {updated.name} -> {updated.chunks} 个检索单元{skipped}")
        else:
            print(f"[失败] {updated.name} -> {updated.error}")


def cmd_list(_: argparse.Namespace) -> None:
    database.init_db()
    docs = database.list_all()
    if not docs:
        print("知识库暂无文档")
        return
    print(f"{'ID':<14}{'状态':<10}{'分块':>5}  {'上传时间':<20}名称")
    for d in docs:
        print(f"{d.id:<14}{d.status:<10}{d.chunks:>5}  {d.uploaded_at:<20}{d.name}")
        if d.error:
            print(f"{'':<14}{'':<10}{'':>5}  {'':<20}  ↳ {d.error}")


def cmd_delete(args: argparse.Namespace) -> None:
    database.init_db()
    if kb.delete(args.doc_id, tool=None):
        print(f"已删除：{args.doc_id}")
    else:
        print(f"未找到：{args.doc_id}")
        sys.exit(1)


def cmd_rebuild(_: argparse.Namespace) -> None:
    database.init_db()
    stats = kb.rebuild()
    print(f"索引已重建：{stats['chunks']} 个分块 / {stats['documents']} 份文档")


def cmd_checkllm(_: argparse.Namespace) -> None:
    from app.core.llm.client import check

    info = check()
    print(f"模型    ：{info['model']}")
    print(f"地址    ：{info['base_url']}")
    print(f"密钥    ：{info['api_key']}")
    print(f"连通性  ：{'通过' if info['ok'] else '失败'}")
    print(f"返回/原因：{info['detail']}")
    sys.exit(0 if info["ok"] else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="企业知识库助手")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="启动统一知识库服务（可选登录）")
    p.add_argument("--host", default="", help="监听主机（默认 config.yaml server.host）")
    p.add_argument("--port", type=int, default=0, help="监听端口（默认 config.yaml server.port）")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("ingest", help="批量导入文件或目录")
    p.add_argument("path")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("list", help="查看文档列表")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("delete", help="按文档 ID 删除")
    p.add_argument("doc_id")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("reanalyze", help="按新规则重新分析文档（摘要 + 主要内容判定）")
    p.add_argument("doc_id", nargs="?", default="", help="缺省时处理全部文档")
    p.set_defaults(func=cmd_reanalyze)

    p = sub.add_parser("reindex", help="重建索引")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("checkllm", help="检查模型配置与连通性")
    p.set_defaults(func=cmd_checkllm)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
