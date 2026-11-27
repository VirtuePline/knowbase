"""企业知识问答；支持方案提取，所有资料服从当前身份权限。"""
from __future__ import annotations

import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.config import settings
from app.core import network_policy
from app.core.production.answers import production_response, render_report
from app.core.production.intent import ProductionQuery, parse_production_question
from app.core.production.store import ProductionStoreError, query_production
from app.core.answer.context import build_hit_context, is_stats_question, render_excerpts, source_snippet, table_to_markdown
from app.core.answer.solutions import extract_solutions, render_solutions
from app.core.answer.references import append_references, attach_reference_sources
from app.core.ingest.chunker import is_title_row
from app.core.retrieval.relevance import filter_fault_hits, manual_code_match
from app.core.llm.client import (
    LLMError,
    SolutionDraft,
    complete as llm_complete,
    draft_solution,
    rewrite_query,
)
from app.core.search.client import WebSearchError, sanitize_query, search as web_search
from app.core.search.relevance import (
    document_matches_search, filter_web_results, product_context_conflicts, public_document_terms,
)
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
from app.services import access, production_routes
from app.services.retrieval_service import authorized_hits, allowed_document_ids, operations_mode, retrieval


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
    hits = authorized_hits(hits)
    uploads = uploaded_dates([hit.chunk.doc_id for hit in hits])
    sources = [
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
    return attach_reference_sources(hits, sources)


# 统计类问题单次交给模型的台账行数上限；超出截断并在提示与说明中注明
_STATS_MAX_ROWS = 300


# ---------- 正文中的【记录 N】占位转可点击文档引用 ----------

_RECORD_REF = re.compile(r"【记录\s*(\d+)】")
_MODE_DEFINITION = re.compile(r"(?:请问[，, ]*)?(?:什么是|什么叫|何谓|怎么开启|如何开启|怎么设置|如何设置|怎么使用|如何使用)?"
                              r"(?P<mode>[一-鿿]{1,8}模式)(?:是什么|是什么意思|指什么|什么意思|"
                              r"怎么用|如何使用|怎么开启|如何设置)?")
_GENERIC_MODE_NAMES = {
    "快速", "普通", "标准", "连续", "自动", "手动", "低功耗", "省电", "高速", "单次", "批量",
    "触发", "静音", "调试", "安全", "学习", "高级", "兼容", "性能", "默认", "节能", "高性能",
}
_CONTEXT_POINTER = re.compile(r"^(?:请问[，, ]*)?(?:(?:这|那)(?:个|项|种|台|款)"
                              r"(?:的|是|怎么|如何|能|会|有|指|功能|模式)|"
                              r"它(?:的|是|怎么|如何|能|会|有|支持|需要)|该设备|"
                              r"(?:上面|前面|刚才)(?:提到的|的))")
_SHORT_FOLLOWUP = re.compile(r"(?:那|那么|然后)?(?:要|应该)?(?:怎么|如何|怎样)"
                             r"(?:设置|配置|开启|关闭|启用|停用|使用|切换|连接|操作|升级|安装)"
                             r"(?:一下|呢|啊|吗)?[？?。\s]*")
_PRODUCT_FEATURE = re.compile(
    r"(?:快速|普通|标准|连续|自动|手动|低功耗|省电|高速|单次|批量|触发|静音|工作|扫描|传输)模式|"
    r"(?:有哪些|哪些|哪种|支持|切换)(?:工作)?模式|"
    r"(?:通讯|通信|连接|数据)?接口|(?:通讯|通信|传输)协议|串口|蓝牙|\b(?:USB|RS[- ]?232|Wi-?Fi)\b|"
    r"(?:传输|通信|通讯|无线|扫码|扫描|识读|读取)距离|码制|条码|二维码|解码|"
    r"技术参数|规格|分辨率|精度|续航|电池|充电|功率|供电|电压|尺寸|重量|防护等级",
    re.I,
)
_OTHER_TOPIC = re.compile(
    r"换个话题|另外一个问题|另一个问题|不说这个|股票|基金|投资|汇率|天气|菜谱|球赛|"
    r"数据库|编程语言|商业模式|设计模式|大语言模型|大模型|人工智能|机器学习|深度学习",
    re.I,
)
_FEATURE_WORDS = r"输出|读取|识别|解码|响应|扫描|传输|处理|打印|显示|连接|查询|播放"


def _followup_documents(question: str, history: list[dict] | None,
                        rejected_doc_ids: list[str]) -> tuple[dict[str, str], str, str]:
    """Resolve an underspecified follow-up against the immediately preceding authorized sources."""
    previous = next((turn for turn in reversed(history or []) if turn.get("role") == "assistant"), {})
    docs = previous.get("source_docs") or []
    mode = _MODE_DEFINITION.fullmatch(question.strip("？?。 "))
    mode_term = mode["mode"] if mode else ""
    if (mode_term and mode_term.removesuffix("模式") not in _GENERIC_MODE_NAMES
            and mode_term not in previous.get("content", "")):
        mode_term = ""
    feature_followup = (len(question) <= 80 and bool(_PRODUCT_FEATURE.search(question))
                        and not _OTHER_TOPIC.search(question))
    if not docs or (not mode_term and not _CONTEXT_POINTER.match(question)
                    and not _SHORT_FOLLOWUP.fullmatch(question) and not feature_followup):
        return {}, "", mode_term
    allowed = allowed_document_ids()
    rejected = set(rejected_doc_ids)
    scoped_docs: dict[str, str] = {}
    document_terms = ""
    for source in docs:
        doc_id = source.get("doc_id")
        if not doc_id or doc_id in rejected or (allowed is not None and doc_id not in allowed):
            continue
        name = source.get("doc_name") or ""
        terms = public_document_terms(name)
        if product_context_conflicts(question, name, terms):
            continue
        scoped_docs[doc_id] = name
        if not document_terms:
            document_terms = terms
    # Several different products in the previous answer are not a safe public search topic.
    topics = {public_document_terms(name) for name in scoped_docs.values()} - {""}
    if len(topics) > 1:
        document_terms = ""
    return scoped_docs, document_terms, mode_term


def _current_manual_document(question: str, hits: list[ScoredChunk]) -> tuple[dict[str, str], str]:
    """Use a current manual only when the strongest hit gives a clear product topic."""
    if not hits or hits[0].context_only:
        return {}, ""
    best = hits[0]
    chunk = best.chunk
    if chunk.doc_type.lower() != "pdf" and not re.search(r"说明书|手册|manual", chunk.doc_name, re.I):
        return {}, ""
    document_terms = public_document_terms(chunk.doc_name)
    passage = f"{chunk.heading}\n{chunk.text}\n{table_to_markdown(chunk.table or [])}"
    if not document_terms or not document_matches_search(question, chunk.doc_name, passage, document_terms):
        return {}, ""
    for hit in hits[1:4]:
        if hit.chunk.doc_id != chunk.doc_id and hit.score >= best.score * .85:
            competing_terms = public_document_terms(hit.chunk.doc_name)
            if competing_terms and competing_terms != document_terms:
                return {}, ""
    return {chunk.doc_id: chunk.doc_name}, document_terms


def _link_kb_records(text: str, hits: list[ScoredChunk]) -> str:
    """把回答正文里的【记录 N】占位替换为文档名引用（doc://，web 端可点击打开预览）。

    编号与模型上下文 render_hits/render_excerpts 的记录序号一致；越界编号原样保留。
    """
    if not text or "【记录" not in text or not hits:
        return text

    def repl(match: re.Match) -> str:
        index = int(match.group(1))
        if not 1 <= index <= len(hits):
            return match.group(0)
        chunk = hits[index - 1].chunk
        name = (chunk.doc_name or "").strip() or "未命名记录"
        return f"[{name}](doc://{chunk.doc_id})"

    return _RECORD_REF.sub(repl, text)


def _ledger_stats(text: str, hits: list[ScoredChunk], category: str,
                  history: list[dict], *, search_query: str = "") -> AnswerResponse | None:
    """统计/清单类问题：拼出命中表格所属的完整工作表，让模型基于全量数据计算。

    检索窗口每次只带回十几行片段，"有几台设备没有IP"这类全表统计必须读整张表，
    再让模型逐行核对后给出数字。统计意图允许来自改写后的查询（追问"那几台里
    还有几个没有MAC的"原话不含量词）。模型答不上（NO_ANSWER / 异常）时返回
    None，落回常规流程（方案提取 → 问答 → 摘录），不把失败伪装成回答。
    """
    hits = authorized_hits(hits)
    anchor = next((h for h in hits if h.chunk.table), None)
    if anchor is None or not settings.llm_available or not network_policy.external_apis_enabled():
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
    if (not reply or "NO_ANSWER" in reply.upper()[:200]) and network_policy.external_apis_enabled():
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
    sources = attach_reference_sources([anchor], sources)
    return AnswerResponse(
        status="ok", answer=_link_kb_records(reply, [anchor]), category=category, sources=sources,
        llm_used=True, answer_origin="knowledge_base", solution_found=False,
        notice=(f"已按{anchor_chunk.heading}完整台账（{scope}）统计；"
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
              reject_docs: list[str] | None = None,
              allowed_doc_ids: set[str] | None = None) -> list[ScoredChunk]:
    # vendor 非空时检索层已把该厂商的文档置顶，这里只做相关性门槛过滤
    # fast=True 跳过 Cross-Encoder 精排（省 2~3 秒），按 BM25 + 向量融合顺序给结果
    stats = is_stats_question(question)
    # 统计类问题放宽召回宽度：台账窗口块的关键词覆盖低，常规 top_k 的候选常被
    # 正文含"IP"等字样的故障记录占满，台账反而进不了候选
    top_k = max(settings.top_k, 12) if stats else settings.top_k
    hits = retrieval.search(question, top_k=top_k, vendor=vendor, use_rerank=not fast,
                            allowed_doc_ids=allowed_doc_ids)
    hits = _apply_answer_penalties(hits, reject_docs or [])
    if not hits:
        return []
    code_pages = {hit.chunk.id for hit in hits if manual_code_match(question, hit)}
    if not operations_mode(question):
        # 制度、财务、人事等知识没有“同类故障”语义，关键词召回可独立支持原文查询。
        return [hit for hit in hits if hit.chunk.id in code_pages or hit.coverage >= settings.min_coverage
                or hit.semantic >= settings.semantic_threshold or hit.rerank >= settings.rerank_threshold]
    # 相关性门槛：rerank / 语义相似度单路强信号即可放行；仅关键词覆盖达标时
    # 要求语义不低于下限（semantic_floor）双确认——防止只碰上「网页」「系统」这类
    # 通用词的不相关记录被当答案返回。rerank 是逐对打分的 Cross-Encoder，能识别
    # 「231 网页打不开」与「234 网页报错记录」这类语义相近但编号不同的场景；
    # 绝对分整体偏低，故阈值远低于语义相似度。
    best_coverage = max(h.coverage for h in hits)
    best_semantic = max(h.semantic for h in hits)
    best_rerank = max(h.rerank for h in hits)
    if (not code_pages and best_coverage < settings.min_coverage
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
            if h.chunk.id in code_pages or h.rerank >= settings.rerank_threshold
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


def _render_plan(plan: SolutionDraft, *, web: bool = False) -> str:
    parts = ["### 联网参考方案" if web else "### 解决方案"]
    parts.extend(f"{i}. {step}" for i, step in enumerate(plan.steps, start=1))
    if plan.verification:
        parts.append(f"\n**验证方法 / 记录结果**：{plan.verification}")
    if plan.limitations:
        parts.append(f"\n**适用条件与缺失信息**：{plan.limitations}")
    return "\n".join(parts)


def _kb_excerpts(question: str, category: str, hits: list[ScoredChunk], notice: str,
                 *, focus_question: str = "") -> AnswerResponse:
    """回退展示：把知识库相关记录原文直接给用户。

    focus_question 用于摘录的表格筛行：上下文改写补全后的查询（含指代所指的
    设备/字段）比原话更容易命中数据行。
    """
    return AnswerResponse(
        status="not_found", answer=_link_kb_records(render_excerpts(hits, question=focus_question or question), hits),
        category=category,
        sources=_sources(hits, question), llm_used=False, answer_origin="knowledge_base",
        notice=notice)


def _web_fallback(question: str, category: str, hits: list[ScoredChunk],
                  *, model_failed: bool = False, web_enabled: bool | None = None,
                  document_terms: str = "", definition: bool = False,
                  related_notice: str = "") -> AnswerResponse:
    """只把搜索结果当外部参考；没有具体方案时不把网页摘要伪装成答案。

    web_enabled 为本次提问的选择：True 表示用户明确要求联网，联网拿不到结果时
    回退展示知识库相关记录原文；缺省沿用服务端全局开关。
    """
    base = ("知识库中未找到与当前问题直接对应的说明。" if definition
            else "知识库中未找到与当前问题直接对应的处理方案。")

    def excerpts(notice: str) -> AnswerResponse:
        return _kb_excerpts(question, category, hits, " ".join(filter(None, (notice, related_notice))))

    if not network_policy.external_apis_enabled():
        if hits:
            return excerpts("离线查询：以下为本地知识库记录原文。")
        return AnswerResponse(status="not_found", answer="本地知识库中未找到相关内容，请补充关键词或相关资料。",
                              category=category, answer_origin="none", notice="当前为离线查询模式，未调用外部接口。")
    web_on = web_search_runtime_enabled() if web_enabled is None else web_enabled
    if not settings.web_search_available:
        if hits:
            return excerpts("联网搜索未配置（缺少搜索密钥），以下为知识库相关记录原文，请按需取用。")
        return AnswerResponse(
            status="not_found", answer=base + "请补充设备型号、错误码或处理记录，以便进一步定位。",
            category=category, sources=_sources(hits),
            notice="联网搜索未配置，尚未取得外部参考。", answer_origin="none")
    if not web_on:
        if hits:
            return excerpts("本次未开启联网搜索，以下为知识库相关记录原文，请按需取用。")
        return AnswerResponse(
            status="not_found", answer=base + "请补充设备型号、错误码或处理记录，以便进一步定位。",
            category=category, sources=_sources(hits),
            notice="联网搜索未开启；如需外部参考，可在提问前打开「联网搜索」开关。", answer_origin="none")
    public_question = sanitize_query(question)
    public_query = f"{document_terms} {public_question}".strip()
    try:
        results = web_search(public_query) if public_question else []
        results = filter_web_results(public_question, results, document_terms=document_terms)
    except WebSearchError:
        if hits:
            return excerpts("联网搜索暂时失败，以下为知识库相关记录原文，请按需取用。")
        return AnswerResponse(
            status="not_found", answer=base + "联网搜索暂时失败，请稍后重试或补充设备型号与错误码。",
            category=category, sources=_sources(hits), answer_origin="none")
    if not results:
        if hits:
            return excerpts("联网搜索也未找到可用参考，以下为知识库相关记录原文，请按需取用。")
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
        if hits:
            return excerpts("未能取得足够的联网参考内容，以下为知识库相关记录原文。")
        return AnswerResponse(status="not_found", answer=base + "未能取得足够的联网参考内容。",
                              category=category, answer_origin="none")
    if definition and settings.llm_available and network_policy.external_apis_enabled():
        try:
            reply = llm_complete(public_query, "\n\n".join(context_blocks), web=True).strip()
        except LLMError:
            reply = ""
        if reply and "NO_ANSWER" not in reply.upper()[:200]:
            return AnswerResponse(
                status="ok", answer=reply, category=category, sources=sources,
                notice="以下为联网参考，请核对产品型号、版本和适用条件。",
                llm_used=True, answer_origin="web", solution_found=False)
    elif settings.llm_available and not model_failed and network_policy.external_apis_enabled():
        try:
            plan = _draft(public_query, "\n\n".join(context_blocks), len(sources))
        except LLMError:
            plan = None
        if plan:
            selected = [sources[ref - 1] for ref in plan.source_records]
            return AnswerResponse(
                status="ok", answer=_render_plan(plan, web=True), category=category,
                sources=selected, notice="以下为联网参考，尚未在本知识库或现场验证；请核对型号、版本和适用条件。",
                llm_used=True, answer_origin="web", solution_found=True)
    if definition:
        return AnswerResponse(
            status="not_found", answer=base + "已找到相关网页，可查看下方来源。",
            category=category, sources=sources, answer_origin="web",
            notice="网页内容仅供参考，请核对产品型号和版本。")
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
    hits = authorized_hits(hits)
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


class AnswerState(TypedDict, total=False):
    question: str
    production_request: ProductionQuery | str
    history: list[dict] | None
    fast_mode: bool | None
    llm_polish: bool
    web_search: bool | None
    rejected_doc_ids: list[str]
    initial_allowed: set[str] | None
    text: str
    ops: bool
    category: str
    fast: bool
    offline: bool
    use_llm: bool
    search_query: str
    retrieval_query: str
    vendor: str
    scoped_docs: dict[str, str]
    document_terms: str
    mode_term: str
    context_hint: str
    hits: list[ScoredChunk]
    related_hits: list[ScoredChunk]
    related_notice: str
    evidence_response: AnswerResponse
    model_hits: list[ScoredChunk]
    model_context: str
    model_failed: bool
    response: AnswerResponse


class AnswerOutput(TypedDict):
    response: AnswerResponse


def _notice(*parts: str) -> str:
    return "；".join(part for part in parts if part)


def _prepare(state: AnswerState) -> AnswerState:
    production = parse_production_question(state["question"] or "")
    if production is not None:
        return {"production_request": production}
    initial_allowed = allowed_document_ids()
    question = state["question"]
    history = state["history"]
    rejected_doc_ids = state["rejected_doc_ids"]
    # 浏览器回传的旧对话不能作为已获授权的证据；只有服务端验证的上下文可复用。
    if not access.is_internal() and not access.history_is_trusted():
        history = []
    rejected_docs = set(rejected_doc_ids)
    if rejected_docs and any(source.get("doc_id") in rejected_docs for turn in history or []
                             for source in turn.get("source_docs", [])):
        history = []
    ops = operations_mode(question or "")
    text = (question or "").strip()
    if not text:
        return {"initial_allowed": initial_allowed, "response": AnswerResponse(
            status="guide", answer="请输入要查询的企业制度、业务流程、文档内容或工作问题。", answer_origin="none")}

    management = is_management_request(text) if ops else text in {
        "上传文件", "上传文档", "删除文件", "删除文档", "查看上传列表", "重建索引", "重建文档库",
    }
    if management:
        return {"initial_allowed": initial_allowed, "response": AnswerResponse(
            status="guide", answer=MANAGEMENT_GUIDE, category="管理指令", answer_origin="none")}

    category, _matched = classify(text) if ops else ("", [])

    # 问题点名了厂商（如「华智」）：归入厂商系统类，检索优先该厂商的文档
    vendor = vendor_hint(text) if ops else ""
    if vendor:
        category = "厂商系统"

    # 开关优先级：本次提问的选择 > 服务端全局设置
    fast_mode = state["fast_mode"]
    fast = fast_mode_enabled() if fast_mode is None else fast_mode
    # 大模型整理：开启后才调用 LLM（方案整理 / 问答 / 结合上文改写 / 联网整理）；
    # 默认关闭——精排 + 规则提取直接出结果，快且稳定。
    offline = not network_policy.external_apis_enabled()
    use_llm = state["llm_polish"] and not fast and not offline

    # 大模型整理开启时结合对话历史改写检索查询：上一轮聊过的设备/字段，追问里
    # 只有"那几台""它"这类指代，单看问题检不到目标记录。改写失败回退原问题，
    # 不影响回答流程（回答阶段的上下文仍由 history 提供给模型）。
    scoped_docs, document_terms, mode_term = _followup_documents(text, history, rejected_doc_ids)
    scoped_ids = set(scoped_docs)
    previous = next((turn for turn in reversed(history or []) if turn.get("role") == "assistant"), {})
    previous_docs = previous.get("source_docs") or []
    new_subject = bool(previous_docs) and (
        bool(_OTHER_TOPIC.search(text)) or all(
            product_context_conflicts(text, source.get("doc_name") or "",
                                      public_document_terms(source.get("doc_name") or ""))
            for source in previous_docs))
    if not scoped_docs and (new_subject or (not mode_term and not _CONTEXT_POINTER.match(text)
                                            and re.search(r"模式|\bmode\b", text, re.I))):
        history = []
    search_query = text
    if use_llm and history and settings.llm_available:
        try:
            search_query = rewrite_query(text, history)
        except LLMError:
            search_query = text
    qualifier = mode_term.removesuffix("模式")
    if mode_term and mode_term not in search_query:
        search_query = text
    retrieval_query = f"{mode_term} {qualifier}" if scoped_ids and mode_term and search_query == text else search_query
    # 改写补出的厂商名也参与检索置顶；分类仍按工程师原话判定
    vendor = (vendor_hint(text) or vendor_hint(search_query)) if ops else ""
    context_hint = "已结合上一轮对话补全检索关键词" if search_query != text or scoped_ids else ""

    return {
        "initial_allowed": initial_allowed, "text": text, "history": history, "ops": ops, "category": category,
        "fast": fast, "offline": offline, "use_llm": use_llm, "scoped_docs": scoped_docs,
        "document_terms": document_terms, "mode_term": mode_term, "search_query": search_query,
        "retrieval_query": retrieval_query, "vendor": vendor, "context_hint": context_hint,
    }


def _retrieve(state: AnswerState) -> AnswerState:
    scoped_ids = set(state["scoped_docs"])
    scope = {"allowed_doc_ids": scoped_ids} if scoped_ids else {}
    hits = authorized_hits(_top_hits(
        state["retrieval_query"], state["vendor"], fast=state["fast"],
        reject_docs=state["rejected_doc_ids"], **scope,
    ))
    return {"hits": [hit for hit in hits if hit.chunk.doc_id in scoped_ids] if scoped_ids else hits}


def _assess_evidence(state: AnswerState) -> AnswerState:
    text, search_query = state["text"], state["search_query"]
    ops, mode_term = state["ops"], state["mode_term"]
    hits = state["hits"]
    # 排名高不代表同一种故障。所有作答、模型与摘录路径共用此门禁，
    # 也核对原始问题，避免历史改写把“电脑打不开”带回上一轮的音响故障。
    if ops:
        hits = filter_fault_hits(search_query, hits)
        if search_query != text:
            hits = filter_fault_hits(text, hits)
    related_hits: list[ScoredChunk] = []
    related_notice = ""
    if mode_term:
        passages = [(hit, f"{hit.chunk.heading}\n{hit.chunk.text}\n{table_to_markdown(hit.chunk.table or [])}")
                    for hit in hits]
        hits = [hit for hit, passage in passages if mode_term in passage]
        if not hits and state["scoped_docs"]:
            qualifier = mode_term.removesuffix("模式")
            related_feature = re.compile(rf"{re.escape(qualifier)}(?:{_FEATURE_WORDS})")
            matched = [(hit, match.group()) for hit, passage in passages
                       if (match := related_feature.search(passage))]
            related_hits = [hit for hit, _ in matched]
            if matched:
                names = "、".join(f"「{name}」" for name in list(dict.fromkeys(name for _, name in matched))[:3])
                related_notice = f"原文提到{names}，暂未确认是否与「{mode_term}」为同一功能。"
    evidence = extract_solutions(hits, text) if ops and not mode_term else []
    updates: AnswerState = {"hits": hits, "related_hits": related_hits, "related_notice": related_notice}
    if evidence:
        selected = list({item.hit.chunk.id: item.hit for item in evidence}.values())
        evidence_response = AnswerResponse(
            status="ok", answer=render_solutions(evidence), category=state["category"],
            sources=_sources(selected, text), solution_found=True,
            notice=_notice(CONFLICT_NOTICE if conflict_risk(selected) else "", state["context_hint"]))
        updates["evidence_response"] = evidence_response
        if not (state["use_llm"] and settings.llm_available and any(h.chunk.references for h in hits)):
            updates["response"] = evidence_response
    return updates


def _statistics(state: AnswerState) -> AnswerState:
    # 通用问答及摘录回退也使用扩展后的工作表。
    hits = _expand_table_hits(state["hits"])
    stats = _ledger_stats(state["text"], hits, state["category"], state["history"],
                          search_query=state["search_query"])
    return {"hits": hits, "response": stats} if stats is not None else {"hits": hits}


def _build_context(state: AnswerState) -> AnswerState:
    hits = authorized_hits(state["hits"])
    if not hits:
        return {"response": AnswerResponse(
            status="not_found", answer="可访问的知识库记录已变更，请重新提问。", answer_origin="none")}
    context, model_hits = build_hit_context(hits, question=state["text"])
    return {"hits": hits, "model_hits": model_hits, "model_context": append_references(context, model_hits)}


def _generate(state: AnswerState) -> AnswerState:
    text, category, history = state["text"], state["category"], state["history"]
    hits, model_context = state["model_hits"], state["model_context"]
    if state["ops"] and not state["mode_term"]:
        try:
            plan = _draft(text, model_context, len(hits), history)
        except LLMError:
            return {"model_failed": True}
        if plan:
            selected = [hits[ref - 1] for ref in plan.source_records]
            return {"response": AnswerResponse(
                status="ok", answer=_link_kb_records(_render_plan(plan), hits), category=category,
                sources=_sources(selected, text), llm_used=True, solution_found=True,
                notice=CONFLICT_NOTICE if conflict_risk(selected) else "")}
    if not network_policy.external_apis_enabled():
        return {}
    # 没有处理步骤的参数查询也可基于同一批资料作答。
    try:
        reply = llm_complete(text, model_context, history).strip()
    except LLMError:
        return {}
    if not reply or "NO_ANSWER" in reply.upper()[:200]:
        return {}
    return {"response": AnswerResponse(
        status="ok", answer=_link_kb_records(reply, hits), category=category,
        sources=_sources(hits, text), llm_used=True,
        answer_origin="knowledge_base", solution_found=False,
        notice=_notice("以下回答由模型基于知识库相关记录整理；"
                       "模型无法确认的部分请以下方原文来源为准。", state["context_hint"]))}


def _web_reference(state: AnswerState) -> AnswerState:
    text, hits, mode_term = state["text"], state["hits"], state["mode_term"]
    scoped_docs, document_terms = state["scoped_docs"], state["document_terms"]
    if not scoped_docs:
        scoped_docs, document_terms = _current_manual_document(text, authorized_hits(hits))
    if (hits or document_terms) and (document_terms or not (scoped_docs or mode_term)):
        # 用户本次明确开启联网，且知识库有相关记录但未能给出可执行方案：
        # 先走联网参考，联网拿不到结果时回退展示知识库相关记录原文。
        response = _web_fallback(text, state["category"], hits or state["related_hits"],
                                 model_failed=state.get("model_failed", False),
                                 web_enabled=True, document_terms=document_terms, definition=bool(mode_term),
                                 related_notice=state["related_notice"])
        if scoped_docs and not any(source.doc_id for source in response.sources):
            doc_id, name = next((doc_id, name) for doc_id, name in scoped_docs.items()
                                if public_document_terms(name) == document_terms)
            response.sources.append(SourceRef(doc_id=doc_id, doc_name=name, location="用于确定联网搜索主题",
                                              score=0.0, snippet="", scoring_method="search_context"))
        return {"response": response}
    return {}


def _fallback(state: AnswerState) -> AnswerState:
    if "evidence_response" in state:
        return {"response": state["evidence_response"]}
    text, category = state["text"], state["category"]
    hits, related_hits = state["hits"], state["related_hits"]
    if related_hits:
        return {"response": _kb_excerpts(text, category, related_hits, state["related_notice"])}
    if hits:
        # 知识库命中但没有可执行方案（如「某某的 IP 是多少」这类台账查询）：
        # 直接回相关记录原文，不转联网，避免把知识库里已有的信息盖掉。
        notice = "未找到与问题直接对应的处理方案，以下为知识库相关记录原文，请按需取用。"
        if state["offline"]:
            notice = "离线查询：以下为本地知识库记录原文，未调用外部接口。"
        elif not state["ops"]:
            notice = "以下为当前账号可访问的知识库原文；可打开「大模型整理」获取基于文档的回答。"
        elif state["fast"]:
            notice = "快速模式：未整理处理步骤，以下为知识库原文，请按需取用。"
        elif not state["use_llm"]:
            notice = "以下为精排后的知识库记录原文；需要模型整理成方案时，可打开「大模型整理」。"
        notice = _notice(notice, state["context_hint"])
        return {"response": _kb_excerpts(text, category, hits, notice, focus_question=state["search_query"])}
    if not state["ops"]:
        return {"response": AnswerResponse(
            status="not_found", answer="当前可访问的企业知识库中未找到相关内容，请补充关键词或联系文档所属部门。",
            answer_origin="none")}
    if not category:
        # 问题不属于任何知识库领域（无设备/系统/故障关键词）：
        # 回答"不在知识库覆盖范围"，不联网，避免用外部网页回答无关问题。
        return {"response": AnswerResponse(
            status="not_found", answer=FALLBACK_TEXT, answer_origin="none",
            notice="该问题不在知识库覆盖的设备与系统范围内，未做联网搜索。")}
    # 知识库没有任何相关内容：不再做联网搜索——外部网页无法对应本厂设备与环境，
    # 搜回来的资料只会把「没找到」拖成几十秒的等待，最后仍是"暂未找到可靠方案"。
    return {"response": AnswerResponse(
        status="not_found",
        answer="知识库中没有与当前问题相关的记录。请补充设备型号、错误码或处理记录，以便进一步定位。",
        category=category, sources=[], answer_origin="none",
        notice="知识库无相关内容，未做联网搜索。")}


def _production_query(state: AnswerState) -> AnswerState:
    actor = access.current_identity() or {}
    if not actor.get("username") or actor.get("public_only") or actor.get("must_change_password") or actor.get("role") == "guest":
        return {"response": production_response("guide", "请使用已获生产报表权限的账号登录后查询。")}
    if not settings.production_sql_enabled or not settings.production_sql_url or not settings.production_sql_departments:
        return {"response": production_response("guide", "生产报表尚未启用，请联系管理员配置数据源和可查询部门。")}
    departments = set(settings.production_sql_departments)
    if actor.get("role") != "admin":
        departments.intersection_update(actor.get("department_ids", []))
    if not departments:
        return {"response": production_response("guide", "当前账号没有生产报表的部门查询权限。")}
    plan = state["production_request"]
    if isinstance(plan, str):
        return {"response": production_response("guide", plan)}

    question = state["question"]
    try:
        route = production_routes.prepare_route(plan, question, settings)
        try:
            result = query_production(route.plan, department_ids=departments, url=settings.production_sql_url,
                                      view=settings.production_sql_view)
        except ProductionStoreError:
            production_routes.record_execution(route.id, question, route.plan, actor["username"], "error")
            raise
        production_routes.record_execution(route.id, question, route.plan, actor["username"],
                                           "ok" if result.rows else "empty")
        return {"response": render_report(route.plan, result)}
    except production_routes.RouteDisabled:
        return {"response": production_response("guide", "管理员已停用此类生产查询。")}
    except production_routes.RouteUnavailable:
        return {"response": production_response("error", "生产查询路由或历史记录暂时不可用，请稍后重试。")}
    except ProductionStoreError as exc:
        return {"response": production_response("error", str(exc))}


def _finalize(state: AnswerState) -> AnswerState:
    if "production_request" in state or access.is_internal():
        return {}
    # 模型调用期间也可能发生撤权；任何用到的主资料或补充引用被撤权，就不返回旧答案。
    response = state["response"]
    allowed = allowed_document_ids() or set()
    used = {source.doc_id for source in response.sources if source.doc_id}
    used.update(reference.doc_id for source in response.sources for reference in source.references)
    if not (state["initial_allowed"] or set()).issubset(allowed) or not used.issubset(allowed):
        return {"response": AnswerResponse(
            status="not_found", answer="文档访问权限已变更，请重新提问。", answer_origin="none")}
    return {}


def _after_prepare(state: AnswerState) -> Literal["production_query", "finalize", "retrieve"]:
    if "production_request" in state:
        return "production_query"
    return "finalize" if "response" in state else "retrieve"


def _after_generate(state: AnswerState) -> Literal["finalize", "web_reference", "fallback"]:
    if "response" in state:
        return "finalize"
    if "evidence_response" not in state and state["use_llm"] and state["web_search"] is True:
        return "web_reference"
    return "fallback"


def _after_statistics(state: AnswerState) -> Literal["finalize", "build_context", "web_reference", "fallback"]:
    if "response" not in state and state["hits"] and state["use_llm"] and settings.llm_available:
        return "build_context"
    return _after_generate(state)


def _after_evidence(state: AnswerState) -> Literal["finalize", "statistics", "build_context", "web_reference", "fallback"]:
    if ("response" not in state and state["use_llm"] and state["hits"]
            and (is_stats_question(state["text"]) or is_stats_question(state["search_query"]))):
        return "statistics"
    return _after_statistics(state)


def _after_context(state: AnswerState) -> Literal["finalize", "generate", "fallback"]:
    if "response" in state:
        return "finalize"
    return "generate" if state["model_hits"] else "fallback"


def _after_web_reference(state: AnswerState) -> Literal["finalize", "fallback"]:
    return "finalize" if "response" in state else "fallback"


_ANSWER_GRAPH = (
    StateGraph(AnswerState, output_schema=AnswerOutput)
    .add_node("prepare", _prepare)
    .add_node("production_query", _production_query)
    .add_node("retrieve", _retrieve)
    .add_node("assess_evidence", _assess_evidence)
    .add_node("statistics", _statistics)
    .add_node("build_context", _build_context)
    .add_node("generate", _generate)
    .add_node("web_reference", _web_reference)
    .add_node("fallback", _fallback)
    .add_node("finalize", _finalize)
    .add_edge(START, "prepare")
    .add_conditional_edges("prepare", _after_prepare)
    .add_edge("production_query", "finalize")
    .add_edge("retrieve", "assess_evidence")
    .add_conditional_edges("assess_evidence", _after_evidence)
    .add_conditional_edges("statistics", _after_statistics)
    .add_conditional_edges("build_context", _after_context)
    .add_conditional_edges("generate", _after_generate)
    .add_conditional_edges("web_reference", _after_web_reference)
    .add_edge("fallback", "finalize")
    .add_edge("finalize", END)
    .compile()
)


def answer(question: str, history: list[dict] | None = None,
           *, fast_mode: bool | None = None, llm_polish: bool = False,
           web_search: bool | None = None,
           rejected_doc_ids: list[str] | None = None) -> AnswerResponse:
    # 问题、对话历史与检索原文只按应用自身的外部接口开关处理，不上传到 LangSmith。
    with tracing_context(enabled=False):
        return _ANSWER_GRAPH.invoke({
            "question": question, "history": history, "fast_mode": fast_mode,
            "llm_polish": llm_polish, "web_search": web_search, "rejected_doc_ids": rejected_doc_ids or [],
        })["response"]
