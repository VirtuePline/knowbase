"""从命中原文提取处理方案；仅整理记录，不补写未记录的操作。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.retrieval.tokenizer import query_terms
from app.core.retrieval.relevance import fault_matches, normalize_fault_label
from app.domain.schemas import ScoredChunk

_SOLUTION = {"处理方案", "解决方案", "处理方法", "解决方法", "处置措施", "维修措施",
             "解决步骤", "操作步骤", "处理措施", "处理步骤", "维修方案", "故障处理",
             "故障排查", "排查步骤", "修复步骤", "处置方法",
             # 运维记录里常见的口语化字段名：「处理过程（尝试了什么办法）：……」
             "处理过程", "处理经过", "处理办法", "解决办法", "处理方式", "解决方式",
             "操作过程", "解决过程", "排查过程", "修复过程", "维修过程", "处理记录",
             "解决措施", "修复措施", "临时措施", "最终方案", "实际处理"}
_RESULT = {"处理结果", "验证结果", "解决结果", "维修结果", "验证情况", "结果"}
_PROBLEM = {"故障现象", "问题描述", "问题现象", "故障描述", "故障问题", "问题", "现象"}
_DEVICE = {"设备", "部件", "设备/部件", "设备名称", "设备型号", "系统/设备", "系统/设备型号", "系统设备型号"}
_IDENTITY = {"编号", "设备编号", "设备名称", "设备", "设备型号", "系统", "系统名称", "模块", "厂商"} | _DEVICE
_METADATA = {"运维人", "维修人", "处理人", "记录人", "运维时间", "处理时间", "记录时间", "日期",
             "时间", "备注", "问题编号", "记录编号", "解决状态", "状态", "原因", "故障原因", "原因分析",
             "根本原因", "更换备件", "更换配件"} | _IDENTITY
_LABELS = _SOLUTION | _RESULT | _PROBLEM | _METADATA
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HEADING = re.compile(r"^\s*(#{1,6})\s*(.+?)\s*#*\s*$")
_FIELD = re.compile(r"^\s*(?:[-*+]\s+|\d+[.、]\s*)?(?:\*\*|__)?"
                    r"(?P<label>[^:：\n]{1,96}?)(?:\*\*|__)?\s*[:：]\s*(?P<body>.*)$")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_QUESTION_FILLER = re.compile(
    r"解决方案|处理方案|处理方法|解决方法|操作步骤|解决步骤|怎么解决|如何解决|怎么处理|如何处理|"
    r"怎么办|怎么|如何|请问|帮我|一下|处理|解决|方案|方法|步骤|问题|故障|设备|记录|查询|请|的|吗|呢")
_GENERIC_TERMS = {"处理", "解决", "方案", "方法", "步骤", "操作", "问题", "故障", "设备",
                  "记录", "相关", "进行", "需要", "已经", "可以", "一下", "帮忙"}
_IDENTIFIER = re.compile(r"[a-z]+(?:[-_.][a-z0-9]+)*[-_.]?\d+(?:[-_.][a-z0-9]+)*|\d+(?:\.\d+){3}|\d{2,}", re.I)
_PLACEHOLDER = re.compile(
    r"^(?:无|暂无|不详|未知|待定|待处理|待解决|待确认|待补充|处理中|已解决|已处理|已完成|"
    r"处理完成|解决完成|正常|恢复正常|已恢复|尚未处理|尚未解决|n/?a|null|none|"
    r"(?:暂无|尚无|没有)(?:具体|详细|明确)?(?:处理|解决|维修|操作)?(?:方案|步骤|措施|方法|记录|内容|信息)?|"
    r"(?:具体|详细|明确)?(?:处理|解决|操作|维修)?(?:方案|步骤|方法|措施)?(?:未记录|未填写|未提供|未说明)|"
    r"(?:未记录|未填写|未提供|未说明)(?:具体|详细|明确)?(?:处理|解决|操作|维修)?(?:方案|步骤|方法|措施|内容|信息)?)$", re.I)
_FIELD_QUERY = re.compile(
    r"ip地址|(?<![a-z])ip(?![a-z])|地址|型号|序列号|物料号|设备号|使用人|负责人|mac", re.I)
_VAGUE_FOLLOWUP = re.compile(
    r"^(?:(?:需|需要|待|有待|尚需|仍需|建议)(?:要)?)?(?:进一步|继续|后续)?"
    r"(?:排查|检查|处理|解决|调查|确认|分析|跟进)(?:原因|问题|故障|情况)?$")


@dataclass
class SolutionEvidence:
    hit: ScoredChunk
    solution: str
    result: str = ""
    problem: str = ""


def _label(value: str) -> str:
    value = re.sub(r"^[\d.、\s]+", "", value)
    return normalize_fault_label(value)


def _kind(label: str) -> str:
    if label in _SOLUTION:
        return "solution"
    if label in _RESULT:
        return "result"
    if label in _PROBLEM:
        return "problem"
    if label in _DEVICE:
        return "device"
    return ""


def _cell(value: str) -> str:
    return _BREAK.sub("\n", str(value or "")).strip()


def _valid_solution(value: str) -> bool:
    # “已解决”等状态只能作为验证结果，不能代替具体处理内容。
    pieces = re.split(r"[，,；;。\n]+", value)
    for piece in pieces:
        clean = re.sub(r"^[\d.、\s]+", "", piece)
        clean = re.sub(r"[\s*`_#。.!！?？\-—/：:]", "", clean)
        if clean and not _PLACEHOLDER.fullmatch(clean) and not _VAGUE_FOLLOWUP.fullmatch(clean):
            return True
    return False


def _terms(value: str) -> set[str]:
    value = _QUESTION_FILLER.sub(" ", value.lower())
    return {term for term in query_terms(value)
            if (len(term) >= 2 or term.isdigit()) and term not in _GENERIC_TERMS}


def _identifier_conflict(requested: set[str], observed: set[str]) -> bool:
    def groups(values):
        grouped: dict[str, set[str]] = {}
        for value in values:
            prefix = "ip" if re.fullmatch(r"\d+(?:\.\d+){3}", value) else re.split(r"\d", value, maxsplit=1)[0]
            grouped.setdefault(prefix, set()).add(value)
        return grouped

    left, right = groups(requested), groups(observed)
    return any(not left[key].intersection(right[key]) for key in left.keys() & right.keys())


def _relevant(records: list[dict[str, str]], question: str, title: str = "") -> list[dict[str, str]]:
    if not question.strip() or not records:
        return records
    terms = _terms(question)
    identifiers = set(_IDENTIFIER.findall(question.lower()))
    scored = []
    for record in records:
        if not fault_matches(question, title=title, problem=record.get("problem", ""),
                             device=record.get("device", "")):
            continue
        subject = f"{record.get('device', '')}\n{record.get('problem', '')}"
        row_ids = set(_IDENTIFIER.findall(subject.lower()))
        if identifiers and row_ids and (not identifiers.intersection(row_ids)
                                       or _identifier_conflict(identifiers, row_ids)):
            continue
        score = len(terms.intersection(_terms(subject)))
        # 编号完整匹配比共享的型号前缀更有区分力。
        score += 3 * len(identifiers.intersection(row_ids))
        scored.append((score, record))
    best = max((score for score, _ in scored), default=0)
    if best:
        return [record for score, record in scored if score == best]
    # 问题是明确的字段 / 编号查询（「某某的IP是多少」「ZX-200 的型号」）而记录里
    # 一个关键词都没对上时，不能把别的记录的处理步骤兜底给它
    if identifiers or _FIELD_QUERY.search(question):
        return []
    # 只有一条原始记录时可沿用检索层相关性；多行表不能猜测目标行。
    return [scored[0][1]] if len(records) == 1 and scored else []


def _table_records(rows: list[list[str]]) -> list[dict[str, str]]:
    if not rows:
        return []
    headers = [_label(str(value)) for value in rows[0]]
    if not any(header in _SOLUTION for header in headers):
        # 兼容“字段 | 内容”的纵向 Markdown 表。
        fields = {_kind(_label(str(row[0]))): _cell(row[1])
                  for row in rows if len(row) >= 2 and _kind(_label(str(row[0])))}
        return [fields] if fields.get("solution") else []
    records = []
    for row in rows[1:]:
        record: dict[str, str] = {}
        for index, header in enumerate(headers):
            if index >= len(row):
                continue
            kind = _kind(header)
            if header in _IDENTITY:
                kind = "problem"
            if kind:
                value = _cell(row[index])
                if value:
                    record[kind] = "\n".join(filter(None, [record.get(kind, ""), value]))
                    if header in _DEVICE:
                        record["device"] = "\n".join(filter(None, [record.get("device", ""), value]))
        if record.get("solution"):
            records.append(record)
    return records


def _markdown_table_blocks(text: str) -> list[tuple[range, list[list[str]]]]:
    tables = []
    current: list[list[str]] = []
    start = 0
    for number, line in enumerate([*text.splitlines(), ""]):
        if "|" in line:
            cells = [cell.strip().replace(r"\|", "|")
                     for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if len(cells) >= 2:
                if not current:
                    start = number
                current.append(cells)
                continue
        if len(current) >= 2 and all(_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in current[1]):
            tables.append((range(start, number), [current[0], *current[2:]]))
        current = []
    return tables


def _text_records(text: str) -> list[dict[str, str]]:
    # 同一行的多个显式字段也分开，字段内容中的分号仍按原文保留。
    names = "|".join(re.escape(label) for label in sorted(_LABELS, key=len, reverse=True))
    text = re.sub(r"[；;]\s*(?=(?:\*\*)?(?:" + names + r")(?:[（(][^）)]*[）)])?(?:\*\*)?\s*[:：])", "\n", text)
    table_lines = {number for span, _ in _markdown_table_blocks(text) for number in span}
    records: list[dict[str, str]] = []
    fields: dict[str, list[str]] = {}
    active = ""
    section_level = 0

    def finish():
        if fields.get("solution"):
            records.append({key: "\n".join(lines).strip() for key, lines in fields.items()})
        fields.clear()

    for number, line in enumerate(text.splitlines()):
        if number in table_lines:  # 表格由独立解析器处理；正文中的管道命令仍原样保留。
            active = ""
            continue
        heading = _HEADING.match(line)
        field = _FIELD.match(line) if not heading else None
        label = _label(heading[2] if heading else field["label"]) if heading or field else ""
        kind = _kind(label)
        if kind:
            if kind in {"problem", "device", "solution"} and fields.get("solution"):
                finish()
            active = kind
            section_level = len(heading[1]) if heading else 0
            fields.setdefault(kind, [])
            if field and field["body"]:
                fields[kind].append(re.sub(r"^(?:\*\*|__)\s*", "", field["body"]))
        elif heading:
            if active == "solution" and section_level and len(heading[1]) > section_level:
                fields[active].append(line)
            else:
                finish()
                active = ""
        elif label in _METADATA:
            active = ""
        elif active:
            fields[active].append(line)
    finish()
    return records


def extract_solutions(hits: list[ScoredChunk], question: str = "") -> list[SolutionEvidence]:
    """提取原文方案；多行台账只返回与问题最相关的行，不从摘要推演操作。"""
    evidence = []
    seen = set()
    for hit in hits:
        groups = []
        if hit.chunk.table:
            groups.append(_table_records(hit.chunk.table))
        groups.extend(_table_records(table) for _, table in _markdown_table_blocks(hit.chunk.text))
        groups.append(_text_records(hit.chunk.text))
        for group in groups:
            for record in _relevant(group, question, title=f"{hit.chunk.doc_name}\n{hit.chunk.heading}"):
                solution = record.get("solution", "").strip()
                if not _valid_solution(solution):
                    continue
                result = record.get("result", "").strip()
                problem = record.get("problem", "").strip()
                key = (hit.chunk.doc_id, solution, result, problem)
                if key not in seen:
                    seen.add(key)
                    evidence.append(SolutionEvidence(hit, solution, result, problem))
    return evidence


def _needs_code_details(solution: str) -> bool:
    return (len(solution) < 120
            and bool(re.search(r"(?:调整|修改|改动|优化|修复).{0,6}(?:代码|程序)|(?:代码|程序).{0,4}(?:调整|修改)", solution))
            and not re.search(r"```|\w+\.(?:py|js|ts|java|cs|cpp|c|sql)\b|第?\d+行|参数.{0,12}[:：=]", solution, re.I))


def render_solutions(evidence: list[SolutionEvidence]) -> str:
    """先展示处理内容，再展示原记录验证与来源；不补写缺失步骤。"""
    if not evidence:
        return ""
    parts = ["### 解决方案"]
    for number, item in enumerate(evidence, start=1):
        if len(evidence) > 1:
            parts.append(f"#### 方案 {number}")
        parts.append(f"**处理：**\n{item.solution}")
        if _needs_code_details(item.solution):
            parts.append("原记录未注明具体修改位置、参数及代码内容，请向相关技术人员确认。")
        if item.result:
            parts.append(f"**原记录结果：** {item.result}")
        chunk = item.hit.chunk
        location = chunk.location or chunk.heading
        parts.append(f"来源：《{chunk.doc_name}》" + (f" · {location}" if location else ""))
    return "\n\n".join(parts)
