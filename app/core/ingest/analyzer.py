"""文档分析：按标题层级和主题结构分段，再为每个结构段生成摘要。

原始 Block → 去页眉页脚 → 按结构归并并拆超长段 → 剔除目录 / 封面 / 版权等非主要内容
→ 结构段摘要 + 全文摘要。

PDF 的原始解析按页产生 Block，但页面只是来源位置，不是内容结构。分析前会把跨页内容按
章节标题重新归并；没有标题线索时，则按自然段落和句子边界分成结构段，不按页总结。

检索只匹配摘要，章节原文挂在 Chunk.text / table 上作为元数据返回。
配置了模型且 analysis.use_llm 为 true 时由模型判断主要内容并写摘要；
模型不可用或返回异常时，自动退回本地规则（挑选记录时间、问题、处理办法、结果和原因的句子）。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from app.config import settings
from app.core.ingest.chunker import _split_text, is_title_row
from app.core.llm.client import LLMError, chat
from app.domain.schemas import Block
from app.domain.taxonomy import DOC_CATEGORY_ORDER, classify_document

# 短于该长度的结构段无需抽取句子，但仍会去除人员、联系方式、流水号等细节
SHORT_SECTION_CHARS = 300
RULE_SUMMARY_CHARS = 160
LLM_SECTION_SUMMARY_CHARS = 150
LLM_DOC_SUMMARY_CHARS = 220
MAX_KEYWORDS = 15

_PAGE_NUMBER = re.compile(
    r"^\s*(?:[-—–]\s*\d+\s*[-—–]|第\s*\d+\s*页(?:\s*[/，,]?\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+|page\s*\d+(?:\s*of\s*\d+)?|\d{1,4})\s*$",
    re.IGNORECASE,
)
_PAGE_LOCATION = re.compile(r"^\s*第\s*(\d+)\s*页\s*$")
_HEADING_PREFIX = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百零〇\d]+[章节篇部分]"
    r"|[一二三四五六七八九十百零〇]+[、.．]"
    r"|[（(][一二三四五六七八九十百零〇\d]+[）)]"
    r"|\d{1,2}(?:\.\d{1,2}){0,4}[、.．\s]"
    r"|附录[A-Za-z0-9一二三四五六七八九十〇]*"
    r")"
)
_NAMED_HEADING = re.compile(
    r"^.{2,32}(?:概述|简介|总则|范围|目的|须知|说明|注意事项|安装步骤|调试步骤|"
    r"操作步骤|使用方法|维护保养|保养周期|故障处理|故障排除|故障诊断|常见故障|"
    r"参数设置|报警处理|附录)$"
)
_DETAIL_LINE = re.compile(
    r"(?m)^\s*(?:[-*•]\s*)?(?:"
    r"运维人|操作人|处理人|提交人|负责人|联系人|审核人|编制人|批准人|"
    r"工单号|单据编号|记录编号|流水号|单号|联系方式|电话|邮箱)\s*[:：][^\n]*(?:\n|$)"
)
_DETAIL_FIELD = re.compile(
    r"(?:运维人|操作人|处理人|提交人|负责人|"
    r"联系人|审核人|编制人|批准人|工单号|单据编号|记录编号|流水号|单号|联系方式)\s*[:：]"
    r"[^；;。\n]*(?:[；;。]|$)"
)
_PHONE_OR_EMAIL = re.compile(r"\b1[3-9]\d{9}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_TOC_HEADING = re.compile(r"^\s*(?:目\s*录|contents|table of contents)\s*$", re.IGNORECASE)
_TOC_LINE = re.compile(r".{2,}?(?:[.。…·\-_\s]{3,})\d{1,4}\s*$")
_BOILERPLATE = re.compile(
    r"版权所有|保留所有权利|all rights reserved|copyright|修订记录|修订历史|版本记录|文档变更记录|变更履历|"
    r"编制[:：]|审核[:：]|批准[:：]|会签|分发范围|密级",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(
    r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+|[A-Za-z]+\d+[A-Za-z0-9]*|\d+[A-Za-z]+[A-Za-z0-9]*"
)
_CUE_WORDS = re.compile(
    r"现象|原因|处理|解决|措施|步骤|排查|报错|报警|故障|异常|注意|警告|禁止|必须|结论|方法|操作|"
    r"结果|尝试|办法|处置|未解决|已解决|"
    r"检查|更换|调整|设置|配置|恢复|失败|错误|超时|参数|周期|标准"
)
_SENTENCE = re.compile(r"[^。！？；!?;\n]+[。！？；!?;]?")

ANALYZE_PROMPT = """你是工厂维保 / MES 运维文档的结构分析助手。输入已经按标题层级和主题归并为若干“结构段”，
不是按页拆开的页面片段。请逐段完成两件事：
1. 判断该结构段是否为主要内容（main）。目录、封面、版权声明、修订 / 审批记录、页眉页脚残留、
   空白标题、纯参考文献 / 联系方式属于非主要内容。故障现象、原因、处理步骤、参数、操作规范、
   维保周期、台账记录属于主要内容。
2. 为主要内容写摘要（summary）：简洁的事件记录，50~120 字，按顺序交代——
   什么时间发生了什么问题 → 尝试了什么办法 → 得到了什么结果 → 是否解决 → 原因是什么。
   原文没有的要素直接省略，不要推测；时间（日期、班次等）必须保留原文表述，不得抹除。
   记录具体故障 / 事件的段落严格按上述要素写；操作规范、手册类段落则写清主题和必须遵守的边界。
   只能使用原文出现的信息，不要整段照抄；人名、联系方式、工单号不要写入摘要，
   具体参数值、阈值和逐条操作流水保留在原文中供追溯。

只输出 JSON，不要任何解释，格式：
{"sections": [{"id": 0, "main": true, "summary": "……"}]}
非主要内容输出 {"id": 3, "main": false}。"""

DOC_SUMMARY_PROMPT = """根据给出的各结构段摘要，写一段 80~160 字的简洁摘要，说明这份文档记录了什么：
什么时间发生了什么问题、尝试了什么办法、得到了什么结果、是否解决、原因是什么。
文档是事件 / 故障记录时严格按这些要素概括；手册、规范类文档则说明覆盖的设备 / 系统与主题。
只使用给出的内容，不要推测；不要按页总结，也不要逐段展开；不要输出标题或列表符号，
直接输出摘要正文。人名、联系方式、工单号不要写入摘要。"""

DOC_CATEGORY_PROMPT = f"""你是工厂维保 / MES 运维知识库的文档分类助手。根据文档名、全文摘要和各结构段摘要，
把文档归入唯一类别，可选值只有：{'、'.join(DOC_CATEGORY_ORDER)}。
判定标准：
- MES系统：MES / WMS / ERP 等信息系统的配置、接口、报工、工单、权限、版本、定时任务、数据同步问题；
- 厂商系统：设备厂商或第三方供应商提供的系统、软件、远程支持、授权与对接问题；
- 硬件问题：设备本体、电气、机械、传感器、相机、伺服等硬件故障、维修与备件更换；
- 网络问题：监控网段、IP、VLAN、网关、交换机、路由、防火墙、DNS 等网络链路、配置与连通性故障；
- 流水线问题：产线 / 流水线级运行问题，例如节拍、堵料卡料、工位协同、上下料、转产；
- 流程问题：作业流程、审批、点检巡检、交接班、培训、规范与制度；
- 其他：确实无法归入以上类别。
判断依据是文档讨论的主要对象（哪套系统、哪类设备、哪类问题），不要因为摘要里出现“处理”“步骤”
等通用词就归为流程问题。只输出 JSON，不要解释：{{"category": "类别名"}}"""


@dataclass
class DocAnalysis:
    summary: str
    sections: list[Block] = field(default_factory=list)   # 主要章节，summary 已填写
    skipped: int = 0                                        # 判定为非主要内容的章节数
    method: str = "rule"                                    # "llm" | "rule"
    category: str = ""                                      # 文档分类，入库后落库用于分组与过滤


# ---------- 预处理 ----------

def _strip_repeated_lines(blocks: list[Block]) -> list[Block]:
    """去掉页码行，以及在多数页面重复出现的页眉页脚行（PDF 常见）。"""
    text_blocks = [b for b in blocks if not b.table and b.text.strip()]
    repeated: set[str] = set()
    if len(text_blocks) >= 3:
        counter = Counter()
        for block in text_blocks:
            counter.update({line.strip() for line in block.text.splitlines() if line.strip()})
        threshold = max(3, len(text_blocks) // 2)
        repeated = {
            line for line, count in counter.items()
            if count >= threshold and len(line) <= 60 and not _looks_like_heading(line)
        }

    cleaned = []
    for block in blocks:
        if block.table:
            cleaned.append(block)
            continue
        lines = [
            line for line in block.text.splitlines()
            if line.strip() not in repeated and not _PAGE_NUMBER.match(line)
        ]
        cleaned.append(replace(block, text="\n".join(lines).strip()))
    return cleaned


def _looks_like_heading(line: str) -> bool:
    """识别 PDF / 纯文本中常见的章节标题行，而不是把每个页面当成章节。"""
    text = line.strip()
    if not 2 <= len(text) <= 60:
        return False
    if text.startswith(("-", "*", "•", "·", "…")) or _PAGE_NUMBER.match(text):
        return False
    if text.endswith(("。", "！", "？", "!", "?", "；", ";", "，", ",")):
        return False
    canonical = text.rstrip(":：").strip()
    prefix = _HEADING_PREFIX.match(canonical)
    if prefix:
        return len(canonical[prefix.end():].strip("、.．:： ")) >= 2
    return bool(_NAMED_HEADING.fullmatch(canonical))


def _page_range(pages: list[int]) -> str:
    if not pages:
        return ""
    first, last = min(pages), max(pages)
    return f"第{first}页" if first == last else f"第{first}-{last}页"


def _pdf_structure(blocks: list[Block]) -> list[Block]:
    """把按页解析的 PDF 内容按标题重新归并；没有标题时按自然段合并。

    location 仍保留页码范围，便于溯源；但结构段本身不再以页为边界，
    后续摘要也不会看到“第 N 页”这种页面信息。
    """
    if not blocks or any(block.table for block in blocks):
        return blocks
    page_numbers = [_PAGE_LOCATION.match(block.location) for block in blocks]
    if not all(page_numbers):
        return blocks

    seed_heading = blocks[0].heading if all(b.heading == blocks[0].heading for b in blocks) else ""
    sections: list[Block] = []
    current: dict = {"heading": seed_heading, "lines": [], "pages": []}
    found_heading = False

    def flush() -> None:
        body = "\n".join(current["lines"]).strip()
        if body:
            sections.append(Block(
                text=body,
                heading=current["heading"],
                location=_page_range(current["pages"]),
            ))

    for page_match, block in zip(page_numbers, blocks):
        page = int(page_match.group(1))
        # PDF 书签/大纲给出的章节标题优先于正文行猜测
        page_heading = block.heading.strip()
        if page_heading and page_heading != current["heading"]:
            flush()
            current = {"heading": page_heading, "lines": [], "pages": [page]}
            found_heading = True
        for raw_line in block.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line == current["heading"] and current["lines"]:
                # 跨页重复出现的章节标题属于版式残留，不另开结构段
                current["pages"].append(page)
            elif _looks_like_heading(line):
                flush()
                current = {"heading": line, "lines": [], "pages": [page]}
                found_heading = True
            else:
                current["lines"].append(line)
                current["pages"].append(page)
    flush()

    if not found_heading or not sections:
        all_lines = [line for block in blocks for line in block.text.splitlines() if line.strip()]
        pages = [int(match.group(1)) for match in page_numbers]
        return [Block(text="\n".join(all_lines), heading=seed_heading, location=_page_range(pages))]
    return sections


def _split_long(blocks: list[Block]) -> list[Block]:
    """超长结构段拆成若干逻辑段，每段单独摘要，避免摘要覆盖过长内容。"""
    limit = settings.analysis_max_section_chars
    result = []
    for block in blocks:
        if block.table or len(block.text) <= limit:
            result.append(block)
            continue
        parts = _split_text(block.text, limit, 0)
        for number, part in enumerate(parts, start=1):
            location = f"{block.location} · 结构段{number}".strip(" ·") if len(parts) > 1 else block.location
            result.append(replace(block, text=part, location=location))
    return result


def is_noise(block: Block) -> bool:
    """规则判定非主要内容：目录、页码残留、短小的版权 / 审批页。"""
    if block.table:
        return False
    text = block.text.strip()
    if not text:
        return True
    lines = [line for line in text.splitlines() if line.strip()]
    if _TOC_HEADING.match(block.heading or "") or (lines and _TOC_HEADING.match(lines[0])):
        return True
    if len(lines) >= 3 and sum(1 for line in lines if _TOC_LINE.match(line)) / len(lines) >= 0.6:
        return True
    if len(text) <= 400 and _BOILERPLATE.search(text) and not _CUE_WORDS.search(text):
        return True
    return False


# ---------- 规则摘要 ----------

def keywords_of(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in _IDENTIFIER.finditer(text):
        token = match.group(0).strip("-_./")
        has_digit = any(c.isdigit() for c in token)
        has_alpha = any(c.isalpha() for c in token)
        if len(token) >= 2 and has_digit and (has_alpha or token.count(".") >= 2):
            seen.setdefault(token, None)
        if len(seen) >= MAX_KEYWORDS:
            break
    return list(seen)


def _compact(text: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()


def _limit_summary(text: str, limit: int) -> str:
    """超长摘要优先截在完整句子处，避免只砍半个操作步骤。"""
    if len(text) <= limit:
        return text
    cut = max(text.rfind(mark, 0, limit + 1) for mark in "。；！？")
    if cut >= limit // 2:
        return text[:cut + 1]
    return text[:limit].rstrip("；;，, ") + "。"


def _generalize_summary(text: str) -> str:
    """去掉人名、联系方式、单号等个案信息，保留时间、问题、处理办法、结果和原因。"""
    cleaned = _DETAIL_LINE.sub("", text)
    cleaned = _DETAIL_FIELD.sub("", cleaned)
    cleaned = _PHONE_OR_EMAIL.sub("联系方式", cleaned)
    cleaned = re.sub(r"[；;]{2,}", "；", cleaned)
    cleaned = re.sub(r"[，,]{2,}", "，", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(" \n；;，,")


def rule_summary(block: Block) -> str:
    """不调模型时的结构段摘要：概括首句与问题、原因、处理、规范相关的句子。"""
    if block.table:
        return table_summary(block)
    text = _compact(_generalize_summary(block.text))
    if not text:
        return block.heading or "该结构段记录了一组相关说明"
    if len(text) <= SHORT_SECTION_CHARS:
        return _limit_summary(text, RULE_SUMMARY_CHARS)

    sentences = [s.strip() for s in _SENTENCE.findall(text) if len(s.strip()) >= 4]
    if not sentences:
        return _limit_summary(text, RULE_SUMMARY_CHARS)

    def score(item: tuple[int, str]) -> float:
        index, sentence = item
        value = 2.0 * len(keywords_of(sentence)) + len(_CUE_WORDS.findall(sentence))
        if index == 0:
            value += 2.5
        return value

    ranked = sorted(enumerate(sentences), key=score, reverse=True)
    chosen: list[int] = []
    used = 0
    for index, sentence in ranked:
        if used + len(sentence) > RULE_SUMMARY_CHARS and chosen:
            continue
        chosen.append(index)
        used += len(sentence)
    return _limit_summary("".join(sentences[i] for i in sorted(chosen)), RULE_SUMMARY_CHARS)


def table_summary(block: Block) -> str:
    rows = block.table or []
    if not rows:
        return ""
    data = rows[1:] if is_title_row(rows) else rows   # 首行是表标题时跳过，字段行才是表头
    header = [c for c in (rows[1] if is_title_row(rows) else rows[0]) if c]
    name = block.heading or "表格"
    return f"{name}：结构化记录表，共 {max(len(data), 0)} 条记录，字段包括：{'、'.join(header)}"


def _rule_doc_summary(doc_name: str, sections: list[Block]) -> str:
    titles = []
    for block in sections:
        title = block.heading
        if title and title not in titles:
            titles.append(title)
    head = f"《{doc_name}》按文档结构组织"
    if titles:
        head += "，主要涵盖：" + "、".join(titles[:10]) + ("等主题" if len(titles) > 10 else "")
    first = next((b.summary for b in sections if b.summary), "")
    return f"{head}。{first[:100]}" if first else head + "。"


# ---------- 模型分析 ----------

def _parse_json(reply: str) -> dict:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON")
    data = json.loads(reply[start:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ValueError("JSON 缺少 sections")
    return data


def _batches(items: list[tuple[int, Block]]) -> list[list[tuple[int, Block]]]:
    budget = max(1000, settings.analysis_batch_chars)
    batches: list[list[tuple[int, Block]]] = []
    current: list[tuple[int, Block]] = []
    used = 0
    for item in items:
        size = len(item[1].text)
        if current and used + size > budget:
            batches.append(current)
            current, used = [], 0
        current.append(item)
        used += size
    if current:
        batches.append(current)
    return batches


def _analyze_batch(doc_name: str, batch: list[tuple[int, Block]]) -> dict[int, dict]:
    parts = []
    for index, block in batch:
        meta = f"结构段：{block.heading or '未命名正文段'}"
        parts.append(f"【#{index}】{meta}\n{block.text}")
    user = f"文档：《{doc_name}》\n\n" + "\n\n".join(parts)
    data = _parse_json(chat(ANALYZE_PROMPT, user))

    valid = {index for index, _ in batch}
    result: dict[int, dict] = {}
    for item in data["sections"]:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if index in valid:
            result[index] = item
    return result


def _llm_summary(item: dict, block: Block) -> str:
    summary = str(item.get("summary") or "").strip()
    if not summary:
        return rule_summary(block)
    return _limit_summary(_generalize_summary(summary), LLM_SECTION_SUMMARY_CHARS)


def _llm_doc_summary(doc_name: str, sections: list[Block]) -> str:
    lines = []
    used = 0
    for block in sections:
        line = f"- {block.heading or '正文段'}：{block.summary}"
        if used + len(line) > settings.analysis_batch_chars:
            break
        lines.append(line)
        used += len(line)
    reply = chat(DOC_SUMMARY_PROMPT, f"文档：《{doc_name}》\n" + "\n".join(lines)).strip()
    if not reply:
        raise ValueError("全文摘要为空")
    return _limit_summary(_generalize_summary(reply), LLM_DOC_SUMMARY_CHARS)


def _use_llm() -> bool:
    return settings.analysis_use_llm and settings.llm_available


# ---------- 文档分类 ----------

def _json_object(reply: str) -> dict:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON")
    data = json.loads(reply[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    return data


def _llm_doc_category(doc_name: str, summary: str, sections: list[Block]) -> str:
    lines = []
    used = 0
    for block in sections:
        line = f"- {block.heading or '正文段'}：{block.summary}"
        if used + len(line) > settings.analysis_batch_chars:
            break
        lines.append(line)
        used += len(line)
    user = f"文档：《{doc_name}》\n全文摘要：{summary}\n结构段摘要：\n" + "\n".join(lines)
    data = _json_object(chat(DOC_CATEGORY_PROMPT, user))
    category = str(data.get("category") or "").strip()
    if category not in DOC_CATEGORY_ORDER:
        raise ValueError("模型返回了未知分类")
    return category


def _rule_doc_category(doc_name: str, summary: str, sections: list[Block]) -> str:
    headings = [block.heading for block in sections if block.heading]
    summaries = [block.summary for block in sections if block.summary]
    return classify_document(doc_name, headings, summaries, summary)


def _doc_category(doc_name: str, summary: str, sections: list[Block]) -> str:
    """优先让模型分类；不可用或返回异常时退回关键词规则。"""
    if _use_llm():
        try:
            return _llm_doc_category(doc_name, summary, sections)
        except (LLMError, ValueError):
            pass
    return _rule_doc_category(doc_name, summary, sections)


# ---------- 入口 ----------

def analyze(blocks: list[Block], doc_name: str) -> DocAnalysis:
    # 先去版式噪声，再把 PDF 的页面块按标题/主题归并成结构段，最后才处理超长段。
    prepared = _split_long(_pdf_structure(_strip_repeated_lines(blocks)))
    prepared = [b for b in prepared if b.text.strip() or b.table]
    if not prepared:
        return DocAnalysis(summary="", sections=[], skipped=0)

    noise = {i for i, b in enumerate(prepared) if is_noise(b)}
    if len(noise) == len(prepared):   # 全部被判为非主要内容时宁可全保留，避免整份文档检索不到
        noise = set()

    decisions: dict[int, dict] = {}
    method = "rule"
    if _use_llm():
        candidates = [(i, b) for i, b in enumerate(prepared) if i not in noise and not b.table]
        for batch in _batches(candidates):
            try:
                decisions.update(_analyze_batch(doc_name, batch))
                method = "llm"
            except (LLMError, ValueError):
                # 单批失败只影响这一批，这些章节退回规则摘要
                continue

    llm_skipped = {i for i, d in decisions.items() if d.get("main") is False}
    if len(llm_skipped | noise) >= len(prepared):
        llm_skipped = set()

    sections: list[Block] = []
    for index, block in enumerate(prepared):
        if index in noise or index in llm_skipped:
            continue
        if index in decisions:
            summary = _llm_summary(decisions[index], block)
        else:
            summary = rule_summary(block)
        fallback = _generalize_summary(_compact(block.text))[:RULE_SUMMARY_CHARS]
        sections.append(replace(block, summary=summary or fallback))

    summary = ""
    if method == "llm":
        try:
            summary = _llm_doc_summary(doc_name, sections)
        except (LLMError, ValueError):
            summary = ""
    if not summary:
        summary = _rule_doc_summary(doc_name, sections)

    return DocAnalysis(
        summary=summary, sections=sections,
        skipped=len(prepared) - len(sections), method=method,
        category=_doc_category(doc_name, summary, sections),
    )
