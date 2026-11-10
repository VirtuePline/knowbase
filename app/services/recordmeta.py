"""上传表单的可配置项：厂商清单与各问题类型的填写模板。

厂商和模板都存 SQLite 的 vendors / doc_templates 表里，网页端可增删改；
代码里只内置默认模板，后台没有自定义过的类型使用默认模板。
"""
from __future__ import annotations

from app.domain.taxonomy import (
    DOC_CATEGORY_ORDER,
    DOC_OTHER,
    all_doc_categories,
    register_custom_categories,
    unregister_custom_category,
)
from app.services import database

VENDOR_MAX_LEN = 30
CATEGORY_MAX_LEN = 20

# 启动时把库里已有的自定义类型注册进 taxonomy（库不可启动时静默跳过）
try:
    register_custom_categories(database.list_custom_categories())
except Exception:   # noqa: BLE001
    pass

TEMPLATE_CATEGORIES = tuple(name for name in DOC_CATEGORY_ORDER if name != DOC_OTHER)

# 各问题类型的默认填写模板（后台未自定义时使用）
DEFAULT_TEMPLATES: dict[str, str] = {
    "MES系统": "\n".join([
        "## MES系统问题处理",
        "",
        "- 系统 / 模块：",
        "- 现象（遇到什么问题）：",
        "- 处理过程（尝试了什么办法）：",
        "- 结果（是否解决）：",
        "- 原因：",
        "",
    ]),
    "厂商系统": "\n".join([
        "## 厂商系统问题处理",
        "",
        "- 厂商：",
        "- 系统 / 设备型号：",
        "- 现象（遇到什么问题）：",
        "- 处理过程（尝试了什么办法，是否联系厂商）：",
        "- 结果（是否解决）：",
        "- 原因：",
        "",
    ]),
    "硬件问题": "\n".join([
        "## 硬件问题处理",
        "",
        "- 设备 / 部件：",
        "- 报警代码：",
        "- 现象（遇到什么问题）：",
        "- 处理过程（尝试了什么办法，是否更换备件）：",
        "- 结果（是否解决）：",
        "- 原因：",
        "",
    ]),
    "网络问题": "\n".join([
        "## 网络问题处理",
        "",
        "- 监控网段：",
        "- 设备 IP：",
        "- 现象（遇到什么问题）：",
        "- 处理过程（尝试了什么办法）：",
        "- 结果（是否解决）：",
        "- 原因：",
        "",
    ]),
    "流水线问题": "\n".join([
        "## 流水线问题处理",
        "",
        "- 线体 / 工位：",
        "- 现象（遇到什么问题）：",
        "- 处理过程（尝试了什么办法）：",
        "- 结果（是否解决）：",
        "- 原因：",
        "",
    ]),
    "流程问题": "\n".join([
        "## 流程问题处理",
        "",
        "- 涉及流程 / 环节：",
        "- 现象（遇到什么问题）：",
        "- 处理过程（尝试了什么办法）：",
        "- 结果（是否解决）：",
        "- 原因：",
        "",
    ]),
}


# ---------- 厂商 ----------

def _check_vendor(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise ValueError("厂商名称不能为空")
    if len(text) > VENDOR_MAX_LEN:
        raise ValueError(f"厂商名称最多 {VENDOR_MAX_LEN} 个字")
    return text


def list_vendors() -> list[str]:
    return database.list_vendors()


def add_vendor(name: str) -> list[str]:
    """新增厂商，返回更新后的清单。重名直接忽略。"""
    database.add_vendor(_check_vendor(name))
    return list_vendors()


def delete_vendor(name: str) -> list[str]:
    """删除厂商，返回更新后的清单。已有文档上填的厂商值保持不变。"""
    database.delete_vendor(str(name or "").strip())
    return list_vendors()


# ---------- 自定义问题类型 ----------

def list_categories() -> list[str]:
    """全部可选问题类型：内置固定分类 + 后台新增的自定义类型。"""
    return all_doc_categories()


def add_category(name: str) -> list[str]:
    """新增自定义类型，返回更新后的清单。与内置类型重名时直接忽略。"""
    text = str(name or "").strip()
    if not text:
        raise ValueError("类型名称不能为空")
    if len(text) > CATEGORY_MAX_LEN:
        raise ValueError(f"类型名称最多 {CATEGORY_MAX_LEN} 个字")
    if text not in DOC_CATEGORY_ORDER:
        database.add_custom_category(text)
        register_custom_categories([text])
    return list_categories()


def delete_category(name: str) -> list[str]:
    """删除自定义类型；已保存记录上的该标签保留，仅不再出现在可选清单。"""
    text = str(name or "").strip()
    if text in DOC_CATEGORY_ORDER:
        raise ValueError("内置类型不能删除")
    database.delete_custom_category(text)
    unregister_custom_category(text)
    return list_categories()


# ---------- 模板 ----------

def get_templates() -> dict[str, str]:
    """全部类型的模板；后台自定义优先，否则用默认模板。"""
    saved = database.list_templates()
    return {name: saved.get(name) or DEFAULT_TEMPLATES.get(name, "") for name in TEMPLATE_CATEGORIES}


def save_template(category: str, content: str) -> dict[str, str]:
    """保存某类型的模板；内容为空表示恢复默认模板。"""
    name = str(category or "").strip()
    if name not in TEMPLATE_CATEGORIES:
        raise ValueError(f"未知问题类型：{name}，可选值：{'、'.join(TEMPLATE_CATEGORIES)}")
    database.set_template(name, str(content or "").replace("\r\n", "\n"))
    return get_templates()
