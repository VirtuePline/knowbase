"""方案优先：知识库处理方案 → 模型提取原文步骤 → 联网参考 → 明确缺失。"""
from __future__ import annotations

from app.config import settings
from app.core.answer.context import is_stats_question, render_excerpts, render_hits, source_snippet, table_to_markdown
from app.core.answer.solutions import extract_solutions, render_solutions
from app.core.answer.references import append_references
from app.core.ingest.chunker import is_title_row
from app.core.retrieval.relevance import filter_fault_hits
from app.core.llm.client import (
    LLMError,
    SolutionDraft,
    complete as llm_complete,
    draft_solution,
    rewrite_query,
)
from app.core.search.client import WebSearchError, search as web_search
from app.domain.schemas import AnswerResponse, ScoredChunk, SourceRef
from app.domain.taxonomy import (
    CONFLICT_NOTICE,
    FALLBACK_TEXT,
    MANAGEMENT_GUIDE,
    classify,
    conflict_risk,
    is_management_request,
    vendor_hint,
)
from app.services.database import answer_penalty_counts, fast_mode_enabled, uploaded_dates, web_search_runtime_enabled
from app.services.retrieval_service import retrieval


def _source_metrics(hit: ScoredChunk | None) -> dict:
    if hit is None or hit.context_only:
        return {"scoring_method": "context"}
    return {
        "scoring_method": "rrf", "rrf_score": hit.score,
        "keyword_coverage": hit.coverage if hit.bm25_used or hit.coverage > 0 else None,
        "semantic_similarity": hit.semantic if hit.semantic_used or hit.semantic != 0 else None,
        "rerank_score": hit.rerank if hit.rerank_used or hit.rerank != 0 else None,
    }

def _sources(hits: list[ScoredChunk], question: str = "") -> list[SourceRef]:
    uploads = uploaded_dates([hit.chunk.doc_id for hit in hits])
    return [
        SourceRef(
            doc_id=hit.chunk.doc_id,
            doc_name=hit.chunk.doc_name,
            location=hit.chunk.location or hit.chunk.heading,
            score=hit.score,
            snippet=source_snippet(hit, question=question),
            table=hit.chunk.table,
            summary=hit.chunk.summary,
            uploaded_at=uploads.get(hit.chunk.doc_id, ""),
            **_source_metrics(hit),
        )
        for hit in hits
    ]


# 统计类问题单次交给模型的台账行数上限；超出截断并在提示与说明中注明
_STATS_MAX_ROWS = 300


def _ledger_stats(text: str, hits: list[ScoredChunk], category: str,
                  history: list[dict], *, search_query: str = "") -> AnswerResponse | None:
    """统计/清单类问题：拼出命中表格所属的完整工作表，让模型基于全量数据计算。

    检索窗口每次只带回十几行片段，"有几台设备没有IP"这类全表统计必须读整张表，
    再让模型逐行核对后给出数字。统计意图允许来自改写后的查询（追问"那几台里
    还有几个没有MAC的"原话不含量词）。模型答不上（NO_ANSWER / 异常）时返回
    None，落回常规流程（方案提取 → 问答 → 摘录），不把失败伪装成回答。
    """
    anchor = next((h for h in hits if h.chunk.table), None)
    if anchor is None or not settings.llm_available:
        return None
    if not (is_stats_question(text) or is_stats_question(search_query)):
        return None
    anchor_chunk = anchor.chunk
    peers = [c for c in retrieval.index.chunks
             if c.doc_id == anchor_chunk.doc_id
             and c.heading == anchor_chunk.heading and c.table]
    if len(peers) < 2:
        return None   # 单窗口块：现有上下文已覆盖整表，常规流程即可
    header: list[str] | None = None
    rows: list[list[str]] = []
    for chunk in peers:                       # index.chunks 保持文档内的切块顺序
        piece = chunk.table or []
        if header is None and piece:
            header = piece[0]
        rows.extend(piece[1:])
    if header is None or not rows:
        return None
    merged = [header, *rows]
    if is_title_row(merged):                  # 兼容旧索引：首块把表标题行当成了表头
        merged = merged[1:]
    total = len(merged) - 1
    scope = f"共 {total} 行记录" + (f"，此处列出前 {_STATS_MAX_ROWS} 行" if total > _STATS_MAX_ROWS else "")
    table_context = (f"【完整台账】《{anchor_chunk.doc_name}》{anchor_chunk.heading}（{scope}）\n"
                     + table_to_markdown(merged[:1 + _STATS_MAX_ROWS]))
    if anchor_chunk.references:
        table_context = append_references("【记录 1】" + table_context, [anchor])
    guided = (f"{text}\n\n"
              "（这是台账统计类问题：请基于上方完整表格逐行核对后给出数字或清单，并说明统计口径；"
              "目标单元格为空、或填的是「/」「-」「无」等占位符的记录，都视为未登记该字段，"
              "统一表述为「未登记」，不要推断为设备实际没有；问「是否设置/配置」时，"
              "凡未登记具体地址即视为未设置，DHCP 等动态获取方式单独说明；"
              "表中信息不足以完成统计时直接说明，不要猜测。）")
    try:
        reply = llm_complete(guided, table_context, history).strip()
    except LLMError:
        reply = ""
    if not reply or "NO_ANSWER" in reply.upper()[:200]:
        # 模型偶发对同一统计请求回答 NO_ANSWER（服务端波动），重试一次；
        # 完整台账已就绪，多花一次调用换来稳定的全表统计。
        try:
            reply = llm_complete(guided, table_context, history).strip()
        except LLMError:
            reply = ""
    if not reply or "NO_ANSWER" in reply.upper()[:200]:
        return None
    scored_by_id = {hit.chunk.id: hit for hit in hits if not hit.context_only}
    sources = [SourceRef(
        doc_id=chunk.doc_id, doc_name=chunk.doc_name,
        location=chunk.location or chunk.heading,
        score=scored_by_id[chunk.id].score if chunk.id in scored_by_id else 0.0,
        snippet=table_to_markdown((chunk.table or [])[:9]),
        table=chunk.table, summary=chunk.summary,
        **_source_metrics(scored_by_id.get(chunk.id)),
    ) for chunk in peers]
    return AnswerResponse(
        status="ok", answer=reply, category=category, sources=sources,
        llm_used=True, answer_origin="knowledge_base", solution_found=False,
        notice=(f"已按《{anchor_chunk.doc_name}》{anchor_chunk.heading}完整台账（{scope}）统计；"
                "「未登记」指台账中该字段为空，不代表设备实际状态。"),
    )


# 惩罚参数强度：答案来源文档每被用户否决一次，rerank -0.2、语义 -0.05、RRF 排序 -0.01，
# 最多累计 2 次。强相关记录（各路高分）几乎不受影响；压线候选（如语义 0.45、重排 0.13）
# 会因此掉出相关性门槛——惩罚由用户在问答页点「重新回答」触发，存 MongoDB 累计。
_PENALTY_RERANK = 0.2
_PENALTY_SEMANTIC = 0.05
_PENALTY_SCORE = 0.01
_PENALTY_MAX = 2


def _apply_answer_penalties(hits: list[ScoredChunk], reject_docs: list[str]) -> list[ScoredChunk]:
    """应用惩罚参数：本次显式拒绝（重新回答）的文档直接剔除，历史否决文档按次数衰减打分。"""
    rejected = {d for d in reject_docs or [] if d}
    counts: dict = {}
    try:
        counts = answer_penalty_counts({h.chunk.doc_id for h in hits} - rejected)
    except Exception:   # noqa: BLE001  库不可用时不惩罚，回答不中断
        counts = {}
    if not rejected and not counts:
        return hits
    kept = []
    for hit in hits:
        if hit.chunk.doc_id in rejected:
            continue
        n = min(counts.get(hit.chunk.doc_id, 0), _PENALTY_MAX)
        if n:
            hit.rerank = max(0.0, hit.rerank - _PENALTY_RERANK * n)
            hit.semantic = max(0.0, hit.semantic - _PENALTY_SEMANTIC * n)
            hit.score = max(0.0, hit.score - _PENALTY_SCORE * n)
        kept.append(hit)
    return kept


def _top_hits(question: str, vendor: str = "", fast: bool = False,
              reject_docs: list[str] | None = None) -> list[ScoredChunk]:
    # vendor 非空时检索层已把该厂商的文档置顶，这里只做相关性门槛过滤
    # fast=True 跳过 Cross-Encoder 精排（省 2~3 秒），按 BM25 + 向量融合顺序给结果
    stats = is_stats_question(question)
    # 统计类问题放宽召回宽度：台账窗口块的关键词覆盖低，常规 top_k 的候选常被
    # 正文含"IP"等字样的故障记录占满，台账反而进不了候选
    top_k = max(settings.top_k, 12) if stats else settings.top_k
    hits = retrieval.search(question, top_k=top_k, vendor=vendor, use_rerank=not fast)
    hits = _apply_answer_penalties(hits, reject_docs or [])
    if not hits:
        return []
    # 相关性门槛：rerank / 语义相似度单路强信号即可放行；仅关键词覆盖达标时
    # 要求语义不低于下限（semantic_floor）双确认——防止只碰上「网页」「系统」这类
    # 通用词的不相关记录被当答案返回。rerank 是逐对打分的 Cross-Encoder，能识别
    # 「231 网页打不开」与「234 网页报错记录」这类语义相近但编号不同的场景；
    # 绝对分整体偏低，故阈值远低于语义相似度。
    best_coverage = max(h.coverage for h in hits)
    best_semantic = max(h.semantic for h in hits)
    best_rerank = max(h.rerank for h in hits)
    if (best_coverage < settings.min_coverage
            and best_semantic < settings.semantic_threshold
            and best_rerank < settings.rerank_threshold):
        # 统计类问题（"几台设备没有IP"）的关键词是量词与字段名，往往不在台账正文
        # （具体值、设备名）里，通用门槛会把台账全部滤掉。此时放行分数最高的表格块：
        # 统计流程会拼出完整台账交给模型逐行核对，台账与问题无关时模型按协议回答
        # NO_ANSWER，不会编造。
        if stats:
            tables = [h for h in hits if h.chunk.table]
            if tables:
                return tables[:1]
        return []
    kept = [h for h in hits
            if h.rerank >= settings.rerank_threshold
            or h.semantic >= settings.semantic_threshold
            or (h.coverage >= settings.min_coverage and h.semantic >= settings.semantic_floor)]
    if stats:
        # 门槛达标也不够：故障记录（正文提到"设备 IP：10.10.x.x"）分数天然比台账
        # 高，会把台账挤到kept之外，统计流程就只剩几条故障记录可看。把候选中分数
        # 最高的表格块（台账/记录表）置顶，保证统计拿到完整台账。
        tables = [h for h in hits if h.chunk.table]
        if tables:
            best = max(tables, key=lambda h: (h.score, h.semantic, h.rerank))
            if kept[0].chunk.id != best.chunk.id:
                kept = [best] + [h for h in kept if h.chunk.id != best.chunk.id]
    return kept


def _draft(question: str, context: str, count: int,
           history: list[dict] | None = None) -> SolutionDraft | None:
    plan = draft_solution(question, context, history)
    if plan is None:
        return None
    # 不接受模型引用未提供、或已被上下文长度限制截掉的资料编号。
    if any(ref > count or f"【记录 {ref}】" not in context for ref in plan.source_records):
        return None
    return plan


def _render_plan(plan: SolutionDraft, sources: list[SourceRef], *, web: bool = False) -> str:
    parts = ["### 联网参考方案" if web else "### 解决方案"]
    parts.extend(f"{i}. {step}" for i, step in enumerate(plan.steps, start=1))
    if plan.verification:
        parts.append(f"\n**验证方法 / 记录结果**：{plan.verification}")
    if plan.limitations:
        parts.append(f"\n**适用条件与缺失信息**：{plan.limitations}")
    if not web:
        parts.append("\n**来源**：" + "；".join(f"《{s.doc_name}》" for s in sources))
    else:
        parts.append("\n来源见下方网页引用。")
    return "\n".join(parts)


def _kb_excerpts(question: str, category: str, hits: list[ScoredChunk], notice: str,
                 *, focus_question: str = "") -> AnswerResponse:
    """回退展示：把知识库相关记录原文直接给用户。

    focus_question 用于摘录的表格筛行：上下文改写补全后的查询（含指代所指的
    设备/字段）比原话更容易命中数据行。
    """
    return AnswerResponse(
        status="not_found", answer=render_excerpts(hits, question=focus_question or question),
        category=category,
        sources=_sources(hits, question), llm_used=False, answer_origin="knowledge_base",
        notice=notice)


def _web_fallback(question: str, category: str, hits: list[ScoredChunk],
                  *, model_failed: bool = False,
                  web_enabled: bool | None = None) -> AnswerResponse:
    """只把搜索结果当外部参考；没有具体方案时不把网页摘要伪装成答案。

    web_enabled 为本次提问的选择：True 表示用户明确要求联网，联网拿不到结果时
    回退展示知识库相关记录原文；缺省沿用服务端全局开关。
    """
    base = "知识库中未找到与当前问题直接对应的处理方案。"
    web_on = web_search_runtime_enabled() if web_enabled is None else web_enabled
    if not settings.web_search_available:
        if hits:
            return _kb_excerpts(question, category, hits,
                                "联网搜索未配置（缺少搜索密钥），以下为知识库相关记录原文，请按需取用。")
        return AnswerResponse(
            status="not_found", answer=base + "请补充设备型号、错误码或处理记录，以便进一步定位。",
            category=category, sources=_sources(hits),
            notice="联网搜索未配置，尚未取得外部参考。", answer_origin="none")
    if not web_on:
        if hits:
            return _kb_excerpts(question, category, hits,
                                "本次未开启联网搜索，以下为知识库相关记录原文，请按需取用。")
        return AnswerResponse(
            status="not_found", answer=base + "请补充设备型号、错误码或处理记录，以便进一步定位。",
            category=category, sources=_sources(hits),
            notice="联网搜索未开启；如需外部参考，可在提问前打开「联网搜索」开关。", answer_origin="none")
    try:
        results = web_search(question)
    except WebSearchError:
        if hits:
            return _kb_excerpts(question, category, hits,
                                "联网搜索暂时失败，以下为知识库相关记录原文，请按需取用。")
        return AnswerResponse(
            status="not_found", answer=base + "联网搜索暂时失败，请稍后重试或补充设备型号与错误码。",
            category=category, sources=_sources(hits), answer_origin="none")
    if not results:
        if hits:
            return _kb_excerpts(question, category, hits,
                                "联网搜索也未找到可用参考，以下为知识库相关记录原文，请按需取用。")
        return AnswerResponse(
            status="not_found", answer=base + "联网搜索也未找到可用参考，请补充设备型号、错误码或联系负责人员。",
            category=category, sources=_sources(hits), answer_origin="none")
    sources: list[SourceRef] = []
    context_blocks: list[str] = []
    used = 0
    for result in results:
        number = len(sources) + 1
        header = f"【记录 {number}】网页：{result.title}\n链接：{result.url}\n"
        remaining = settings.max_context_chars - used - len(header)
        if remaining < 100:
            break
        content = result.content[:min(remaining, 3000)]
        context_blocks.append(header + content)
        used += len(header) + len(content) + 2
        sources.append(SourceRef(
            doc_id="", doc_name=result.title, location="联网搜索", score=result.score,
            snippet=content[:settings.max_excerpt_chars], kind="web", url=result.url, scoring_method="web"))
    if not sources:
        return AnswerResponse(status="not_found", answer=base + "未能取得足够的联网参考内容。",
                              category=category, answer_origin="none")
    if settings.llm_available and not model_failed:
        try:
            plan = _draft(question, "\n\n".join(context_blocks), len(sources))
        except LLMError:
            plan = None
        if plan:
            selected = [sources[ref - 1] for ref in plan.source_records]
            return AnswerResponse(
                status="ok", answer=_render_plan(plan, selected, web=True), category=category,
                sources=selected, notice="以下为联网参考，尚未在本知识库或现场验证；请核对型号、版本和适用条件。",
                llm_used=True, answer_origin="web", solution_found=True)
    return AnswerResponse(
        status="not_found", answer="### 暂未找到可靠解决方案\n\n" + base
        + "已找到相关网页，但未能从中确认可执行方案，可查看下方来源继续核对。",
        category=category, sources=sources, answer_origin="web",
        notice="网页内容仅供参考，不代表已验证的现场处理方案。")


def _expand_table_hits(hits: list[ScoredChunk]) -> list[ScoredChunk]:
    """把命中表格块扩展为其所属完整工作表的全部窗口块（按 chunk id 去重、保持顺序）。

    统计类问题落到问答/摘录回退时（完整台账统计失败），模型不能再只看检索窗口的
    12 行——那会给出以偏概全的数字（如只数了一个窗口的 8 台）。扩展后每个表按
    文档内窗口顺序完整进入上下文，max_context_chars 仍兜底截断。
    """
    expanded: list[ScoredChunk] = []
    scored_by_id = {hit.chunk.id: hit for hit in hits}
    seen_tables: set[tuple[str, str]] = set()
    for hit in hits:
        chunk = hit.chunk
        if chunk.table and (chunk.doc_id, chunk.heading) not in seen_tables:
            seen_tables.add((chunk.doc_id, chunk.heading))
            peers = [c for c in retrieval.index.chunks
                     if c.doc_id == chunk.doc_id and c.heading == chunk.heading and c.table]
            if peers:
                for peer in peers:   # index.chunks 保持文档内的窗口顺序
                    expanded.append(scored_by_id.get(peer.id) or ScoredChunk(
                        chunk=peer, score=0.0, coverage=0.0, context_only=True))
                continue
        expanded.append(hit)   # 索引中找不到同表窗口（异常情况）时保留原命中
    unique: dict[str, ScoredChunk] = {}
    for hit in expanded:
        unique.setdefault(hit.chunk.id, hit)
    return list(unique.values())


def answer(question: str, history: list[dict] | None = None,
           *, fast_mode: bool | None = None, llm_polish: bool = False,
           web_search: bool | None = None,
           rejected_doc_ids: list[str] | None = None) -> AnswerResponse:
    text = (question or "").strip()
    if not text:
        return AnswerResponse(status="guide", answer="请输入要查询的设备故障现象、MES 报错代码或工单问题。", answer_origin="none")

    if is_management_request(text):
        return AnswerResponse(status="guide", answer=MANAGEMENT_GUIDE, category="管理指令", answer_origin="none")

    category, _matched = classify(text)

    # 问题点名了厂商（如「华智」）：归入厂商系统类，检索优先该厂商的文档
    vendor = vendor_hint(text)
    if vendor:
        category = "厂商系统"

    # 开关优先级：本次提问的选择 > 服务端全局设置
    fast = fast_mode_enabled() if fast_mode is None else fast_mode
    # 大模型整理：开启后才调用 LLM（方案整理 / 问答 / 结合上文改写 / 联网整理）；
    # 默认关闭——精排 + 规则提取直接出结果，快且稳定。
    use_llm = bool(llm_polish) and not fast

    # 大模型整理开启时结合对话历史改写检索查询：上一轮聊过的设备/字段，追问里
    # 只有"那几台""它"这类指代，单看问题检不到目标记录。改写失败回退原问题，
    # 不影响回答流程（回答阶段的上下文仍由 history 提供给模型）。
    search_query = text
    if use_llm and history and settings.llm_available:
        try:
            search_query = rewrite_query(text, history)
        except LLMError:
            search_query = text
    # 改写补出的厂商名也参与检索置顶；分类仍按工程师原话判定
    vendor = vendor_hint(text) or vendor_hint(search_query)
    context_hint = "已结合上一轮对话补全检索关键词" if search_query != text else ""

    def _notice(*parts: str) -> str:
        return "；".join(part for part in parts if part)

    hits = _top_hits(search_query, vendor, fast=fast, reject_docs=rejected_doc_ids or [])
    # 排名高不代表同一种故障。所有作答、模型与摘录路径共用此门禁，
    # 也核对原始问题，避免历史改写把“电脑打不开”带回上一轮的音响故障。
    hits = filter_fault_hits(search_query, hits)
    if search_query != text:
        hits = filter_fault_hits(text, hits)
    evidence = extract_solutions(hits, text)
    evidence_response = None
    if evidence:
        selected = list({item.hit.chunk.id: item.hit for item in evidence}.values())
        evidence_response = AnswerResponse(
            status="ok", answer=render_solutions(evidence), category=category,
            sources=_sources(selected, text), solution_found=True,
            notice=_notice(CONFLICT_NOTICE if conflict_risk(selected) else "", context_hint))
        if not (use_llm and settings.llm_available and any(h.chunk.references for h in hits)):
            return evidence_response
    if not fast and use_llm and hits and (is_stats_question(text) or is_stats_question(search_query)):
        # 统计类问题：无论走完整台账统计还是问答/摘录回退，上下文都需要完整工作表
        hits = _expand_table_hits(hits)
        stats = _ledger_stats(text, hits, category, history, search_query=search_query)
        if stats is not None:
            return stats
    model_failed = False
    if hits and settings.llm_available and use_llm:
        model_context = append_references(render_hits(hits, question=text), hits)
        try:
            plan = _draft(text, model_context, len(hits), history)
        except LLMError:
            plan = None
            model_failed = True
        if plan:
            selected = [hits[ref - 1] for ref in plan.source_records]
            sources = _sources(selected, text)
            return AnswerResponse(
                status="ok", answer=_render_plan(plan, sources), category=category,
                sources=sources, llm_used=True, solution_found=True,
                notice=CONFLICT_NOTICE if conflict_risk(selected) else "")
        if not model_failed:
            # 提取不到"处理方案"不等于无答案：台账统计（如"几台设备没有IP"）、
            # 参数查询这类问题没有操作步骤，但记录原文往往能直接回答。再让模型以
            # 问答模式基于同一批记录作答；记录不足时模型按协议输出 NO_ANSWER，
            # 此时维持原文摘录回退，不把"无答案"伪装成回答。
            try:
                reply = llm_complete(text, model_context, history).strip()
            except LLMError:
                reply = ""
            if reply and "NO_ANSWER" not in reply.upper()[:200]:
                return AnswerResponse(
                    status="ok", answer=reply, category=category,
                    sources=_sources(hits, text), llm_used=True,
                    answer_origin="knowledge_base", solution_found=False,
                    notice=_notice("以下回答由模型基于知识库相关记录整理；"
                                   "模型无法确认的部分请以下方原文来源为准。", context_hint))
    if evidence_response is not None:
        return evidence_response
    if not fast and use_llm and web_search is True and hits:
        # 用户本次明确开启联网，且知识库有相关记录但未能给出可执行方案：
        # 先走联网参考，联网拿不到结果时回退展示知识库相关记录原文。
        return _web_fallback(text, category, hits, model_failed=model_failed, web_enabled=True)
    if hits:
        # 知识库命中但没有可执行方案（如「某某的 IP 是多少」这类台账查询）：
        # 直接回相关记录原文，不转联网，避免把知识库里已有的信息盖掉。
        notice = "未找到与问题直接对应的处理方案，以下为知识库相关记录原文，请按需取用。"
        if fast:
            notice = "快速模式：未整理处理步骤，以下为知识库原文，请按需取用。"
        elif not use_llm:
            notice = "以下为精排后的知识库记录原文；需要模型整理成方案时，可打开「大模型整理」。"
        notice = _notice(notice, context_hint)
        return _kb_excerpts(text, category, hits, notice, focus_question=search_query)
    if not category:
        # 问题不属于任何知识库领域（无设备/系统/故障关键词）：
        # 回答"不在知识库覆盖范围"，不联网，避免用外部网页回答无关问题。
        return AnswerResponse(
            status="not_found", answer=FALLBACK_TEXT, answer_origin="none",
            notice="该问题不在知识库覆盖的设备与系统范围内，未做联网搜索。")
    # 知识库没有任何相关内容：不再做联网搜索——外部网页无法对应本厂设备与环境，
    # 搜回来的资料只会把「没找到」拖成几十秒的等待，最后仍是"暂未找到可靠方案"。
    return AnswerResponse(
        status="not_found",
        answer="知识库中没有与当前问题相关的记录。请补充设备型号、错误码或处理记录，以便进一步定位。",
        category=category, sources=[], answer_origin="none",
        notice="知识库无相关内容，未做联网搜索。")
