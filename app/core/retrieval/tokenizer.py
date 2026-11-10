"""中英混合分词。

- 设备编号、报错码、版本号（ZX-200、E-1024、MES-ERR-1024、3.2.1）整体保留，同时拆出 ≥2 位的组成部分；
- 英文 / 数字按词切分并转小写；
- 中文按语气助词断开后做二元切分（单字片段保留单字）。
"""
from __future__ import annotations

import re

# 复合标识符：字母数字段之间用 - _ . / 连接
_COMPOUND = r"[a-z0-9]+(?:[-_./][a-z0-9]+)+"
_WORD = r"[a-z0-9]+"
_CJK = r"[一-鿿]+"
_TOKEN_RE = re.compile(f"{_COMPOUND}|{_WORD}|{_CJK}")

# 在这些字处断开中文片段，避免生成「么处」「的故」这类无意义二元组
_BREAK_CHARS = re.compile(r"[的了吗呢吧啊呀么哪嘛]")

STOPWORDS = {
    "怎么", "怎样", "如何", "什么", "为什", "为何", "请问", "一下", "可以", "是否",
    "这个", "那个", "这种", "我们", "你们", "他们", "应该", "需要", "能否", "有没",
    "没有", "还是", "或者", "以及", "然后", "现在", "时候", "问题", "情况", "办法",
    "是不", "不是", "有什", "出现", "一直", "总是", "帮我", "告诉", "查一", "查询",
    "怎", "么", "吗", "呢", "啊", "是", "在", "和", "与", "及", "或", "把", "被",
    "给", "让", "有", "要", "会", "就", "都", "也", "还", "又", "我", "你", "他",
    "the", "and", "or", "is", "are", "to", "of", "in", "on", "for",
}


def _cjk_terms(run: str) -> list[str]:
    terms: list[str] = []
    for piece in _BREAK_CHARS.split(run):
        if not piece:
            continue
        if len(piece) == 1:
            terms.append(piece)
        else:
            terms.extend(piece[i:i + 2] for i in range(len(piece) - 1))
    return terms


def tokenize(text: str) -> list[str]:
    """文档侧分词：保留重复，用于词频统计。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group(0)
        if "一" <= token[0] <= "鿿":
            tokens.extend(_cjk_terms(token))
        elif re.fullmatch(_COMPOUND, token):
            tokens.append(token)
            tokens.extend(p for p in re.split(r"[-_./]", token) if len(p) >= 2)
        elif len(token) >= 2 or token.isdigit():
            tokens.append(token)
    return [t for t in tokens if t not in STOPWORDS]


def query_terms(question: str) -> list[str]:
    """查询侧分词：去重并保持出现顺序。"""
    seen: dict[str, None] = {}
    for token in tokenize(question):
        seen.setdefault(token, None)
    return list(seen)
