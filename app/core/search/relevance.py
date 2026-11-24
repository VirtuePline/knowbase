"""Build public search terms from document titles and check returned snippets."""
from __future__ import annotations

import re

from app.core.retrieval.relevance import fault_matches
from app.core.retrieval.tokenizer import query_terms
from app.core.search.client import WebResult


_PUBLIC_VENDORS = (
    re.compile(r"海康机器人|(?<![a-z0-9_])HIKROBOT(?![a-z0-9_])", re.I),
    re.compile(r"海康威视|(?<![a-z0-9_])HIKVISION(?![a-z0-9_])", re.I),
    re.compile(r"斑马技术|(?<![a-z0-9_])Zebra(?![a-z0-9_])", re.I),
    re.compile(r"霍尼韦尔|(?<![a-z0-9_])Honeywell(?![a-z0-9_])", re.I),
    re.compile(r"基恩士|(?<![a-z0-9_])Keyence(?![a-z0-9_])", re.I),
    re.compile(r"康耐视|(?<![a-z0-9_])Cognex(?![a-z0-9_])", re.I),
    re.compile(r"得利捷|(?<![a-z0-9_])Datalogic(?![a-z0-9_])", re.I),
    re.compile(r"西门子|(?<![a-z0-9_])Siemens(?![a-z0-9_])", re.I),
    re.compile(r"欧姆龙|(?<![a-z0-9_])Omron(?![a-z0-9_])", re.I),
    re.compile(r"倍福|(?<![a-z0-9_])Beckhoff(?![a-z0-9_])", re.I),
)
_DEVICE_TYPES = (
    (re.compile(r"(?:无线)?(?:手持)?(?:条码)?(?:扫码枪|扫码器|扫描枪|扫描器|读码器|条码枪)"),
     re.compile(r"\b(?:barcode|bar code|code|handheld)\s+(?:scanner|reader)s?\b", re.I)),
    (re.compile(r"(?:条码|标签)?打印机"), re.compile(r"\bprinters?\b", re.I)),
    (re.compile(r"贴标机"), re.compile(r"\blabell?ing machines?\b", re.I)),
    (re.compile(r"(?:工业|机器视觉)相机"), re.compile(r"\b(?:industrial|machine vision) cameras?\b", re.I)),
    (re.compile(r"交换机"), re.compile(r"\bnetwork switch(?:es)?\b", re.I)),
    (re.compile(r"路由器"), re.compile(r"\brouters?\b", re.I)),
    (re.compile(r"灌装机"), re.compile(r"\bfilling machines?\b", re.I)),
)
_FAST_MODE = re.compile(r"快速模式|\b(?:fast|quick|rapid) mode\b", re.I)
_MODEL_CODE = re.compile(r"(?<![a-z0-9_-])(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*[0-9])"
                         r"[a-z0-9]+(?:[-_][a-z0-9]+)*(?![a-z0-9_-])", re.I)
_QUERY_FILLER = {
    "什么", "是什", "意思", "含义", "定义", "怎么", "处理", "解决", "问题", "方法", "操作",
    "设备", "功能", "信息", "相关", "教程", "使用", "why", "what", "how", "meaning",
}


def public_document_terms(title: str) -> str:
    """Extract public product terms and leave filename suffixes and internal names out."""
    vendor = next((match for name in _PUBLIC_VENDORS if (match := name.search(title))), None)
    product = next((match for names in _DEVICE_TYPES for name in names
                    if (match := name.search(title))), None)
    return " ".join(match.group() for match in (vendor, product) if match)


def product_context_conflicts(question: str, title: str, document_terms: str) -> bool:
    """An explicitly named product must not be replaced by the previous manual's topic."""
    vendors = [name for name in _PUBLIC_VENDORS if name.search(question)]
    products = [names for names in _DEVICE_TYPES if any(name.search(question) for name in names)]
    if any(not name.search(document_terms) for name in vendors):
        return True
    if any(not any(name.search(document_terms) for name in names) for names in products):
        return True
    codes = [code for code in _MODEL_CODE.findall(question)
             if not re.fullmatch(r"(?:USB|RS|IP|WiFi|Bluetooth)[-_]?\d+", code, re.I)]
    if any(code.casefold() not in title.casefold() for code in codes):
        return True
    # A generic follow-up can still explicitly introduce another software product.
    return any(match.group().casefold() not in title.casefold()
               for match in re.finditer(r"(?<![a-z0-9_])(?:OpenAI|ChatGPT|Claude|Gemini)(?![a-z0-9_])",
                                        question, re.I))


def document_matches_search(question: str, title: str, passage: str, document_terms: str) -> bool:
    """Check that a current manual hit can supply product context for a public search."""
    vendors = [name for name in _PUBLIC_VENDORS if name.search(question)]
    products = [names for names in _DEVICE_TYPES if any(name.search(question) for name in names)]
    if product_context_conflicts(question, title, document_terms):
        return False
    if vendors or products:
        return True
    if _FAST_MODE.search(question):
        named_terms = set(query_terms(_FAST_MODE.sub(" ", question))) - _QUERY_FILLER
        named_terms = {term for term in named_terms if len(term) >= 2}
        return (bool(named_terms & set(query_terms(title))) if named_terms
                else bool(_FAST_MODE.search(passage)))
    terms = {term for term in query_terms(question) if len(term) >= 2 and term not in _QUERY_FILLER}
    return len(terms & set(query_terms(passage))) >= max(2, (len(terms) + 1) // 2)


def filter_web_results(query: str, results: list[WebResult], *, document_terms: str = "") -> list[WebResult]:
    """Keep snippets that support the question and, when available, the document's product scope."""
    fast_mode = _FAST_MODE.search(query) is not None
    query_subject = _FAST_MODE.sub(" ", query) if fast_mode else query
    terms = {term for term in query_terms(query_subject) if len(term) >= 2 and term not in _QUERY_FILLER}
    model_codes = {code.casefold() for code in _MODEL_CODE.findall(query)}
    product_types = [names for names in _DEVICE_TYPES
                     if any(name.search(document_terms) for name in names)]
    vendor = next((name for name in _PUBLIC_VENDORS if name.search(document_terms)), None)
    found = []
    for result in results:
        snippet = f"{result.title}\n{result.content}"
        if any(code not in snippet.casefold() for code in model_codes):
            continue
        if any(not any(name.search(snippet) for name in names) for names in product_types):
            continue
        if vendor and not vendor.search(snippet):
            if not product_types or any(name.search(snippet) for name in _PUBLIC_VENDORS):
                continue
        if not fault_matches(query, title=result.title, problem=result.content):
            continue
        matches = terms & set(query_terms(snippet))
        if fast_mode:
            if _FAST_MODE.search(snippet) and (not terms or matches):
                found.append(result)
            continue
        if len(matches) >= min(2, max(1, len(terms))):
            found.append(result)
    return found
