"""问题分类、管理类指令识别与固定兜底话术。"""
from __future__ import annotations

import re

FALLBACK_TEXT = (
    "【知识库未查询到匹配的记录】\n"
    "当前可访问的知识库中暂无直接回答该问题的文档记录。\n"
    "建议你：\n"
    "1. 核对主题、关键词、名称或编号是否输入准确；\n"
    "2. 补充相关制度、业务流程、工作记录或参考资料到知识库；\n"
    "3. 如为新事项，请登记工作记录，后续录入知识库后可查询。"
)

MANAGEMENT_GUIDE = (
    "【操作提示】我不直接执行上传、删除等管理操作，请在系统操作面板中使用对应功能：\n"
    "1. 上传：打开页面顶部「文档管理」标签页，点击「选择文件」，支持 PDF / Word / Excel / CSV / TXT / Markdown；\n"
    "2. 删除：在文档列表对应记录右侧点击「删除」；\n"
    "3. 新增或更新文档后，点击「重建文档库」，使新内容可被检索到。"
)

CONFLICT_NOTICE = "【多条历史记录存在差异，请相关负责人结合实际情况核实】"

# 问题文本中的厂商关键词：命中后按「厂商系统」类问题处理，
# 检索结果里该厂商的文档优先展示（文档名 / 标题 / 摘要 / 原文含厂商词）。
# 厂商清单在「知识分类与模板」维护；这里只登记需要在问答里识别的厂商词。
VENDOR_TERMS: tuple[str, ...] = ("华智",)

# 分类关键词。命中越多得分越高，用于决定回答侧重点与提示，不影响检索。
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "硬件故障排查": [
        "设备", "故障", "报警", "异响", "跳闸", "电机", "传感器", "变频器", "伺服",
        "气缸", "轴承", "温度", "振动", "漏油", "不通电", "停机", "急停", "烧毁",
        "接触器", "继电器", "电源", "风扇", "皮带", "链条", "减速机",
        "贴标机", "灌装机", "相机", "输送线", "机械手", "机器人", "空压机", "泵",
        "阀门", "流量计", "光电", "编码器", "限位", "过载", "卡料", "偏移",
        "电脑", "计算机", "工控机", "笔记本", "音响", "音箱", "喇叭", "开不了机", "无法开机",
    ],
    "软件调试": [
        "调试", "参数", "驱动", "固件", "烧录", "组态", "上位机", "通讯", "串口",
        "网口", "心跳", "地址", "变量", "程序下载", "触摸屏", "HMI",
    ],
    "配件查询": ["配件", "备件", "料号", "厂家", "库存", "更换记录", "订货号"],
    "维保周期": ["维保", "保养", "巡检", "点检", "周期", "润滑", "校准", "到期", "年检"],
    "MES报错排查": ["MES", "报错", "错误代码", "异常", "日志", "提示", "失败", "崩溃", "ERR", "网页"],
    "报工异常": ["报工", "报数", "过站", "报工数量", "产量", "不良", "返工", "重复报工"],
    "流程单流转异常": ["流程单", "派工", "下发", "流转", "卡住", "关闭", "创建流程单", "流程单状态"],
    "接口异常": ["接口", "对接", "ERP", "WMS", "PLC", "报文", "同步", "超时", "回调", "推送", "MQ"],
    "权限问题": ["权限", "账号", "登录", "角色", "授权", "密码", "锁定", "菜单看不到"],
    "系统配置": ["配置", "参数设置", "字典", "基础数据", "班次", "工序", "物料", "条码规则", "工艺路线"],
    "版本与补丁": ["版本", "升级", "补丁", "发布", "回滚", "热更"],
    "定时任务": ["定时任务", "调度", "跑批", "自动任务", "job", "cron", "夜间任务"],
    "数据同步": ["同步", "对账", "数据不一致", "缺失", "重复数据", "补数据"],
}

MES_KEYWORDS = {"MES", "报工", "流程单", "条码", "工序", "班次", "接口", "WMS", "ERP", "PLC"}

# 必须同时出现「动作」和「文件/文档语义」才算管理指令，
# 避免「MES 流程单删除失败」这类业务问题被误判。
_MANAGEMENT_PATTERNS = [
    r"(上传|删除|删掉|移除|清空)[^，。]{0,10}(文件|文档|资料|附件|知识库|资料库)",
    r"(文件|文档|附件|知识库)(的)?(上传|删除|列表|清单|管理)",
    r"上传列表|文件列表|文档列表|知识库列表",
    r"(重新|重建)(文档库|索引|向量库)",
    r"(删除|删掉)[^，。]{0,20}\.(pdf|docx?|xlsx?|xlsm|csv|txt|md)",
    r"\.(pdf|docx?|xlsx?|xlsm|csv|txt|md)[^，。]{0,10}(删除|删掉)",
]

_MANAGEMENT_RE = re.compile("|".join(_MANAGEMENT_PATTERNS), re.IGNORECASE)


def is_management_request(question: str) -> bool:
    """判断是否为文件上传/删除/列表等管理类指令。

    注意：只匹配带文件语义或明确列表语义的表达，避免把
    「流程单删除失败怎么办」这类业务问题误判为管理指令。
    """
    q = question.strip()
    if not q:
        return False
    if _MANAGEMENT_RE.search(q):
        return True
    return q in {"上传", "删除", "查看文件", "文件管理"}


def _upper_mes(text: str) -> str:
    return text.upper() if "mes" in text.lower() else text


def classify(question: str) -> tuple[str, list[str]]:
    """返回 (主分类, 命中分类列表)。无命中时返回空主分类。"""
    q = _upper_mes(question)
    scores: list[tuple[str, int]] = []
    for category, words in CATEGORY_KEYWORDS.items():
        hits = [w for w in words if w.upper() in q.upper()]
        if hits:
            scores.append((category, len(hits)))
    if not scores:
        return "", []
    # MES 类问题优先，避免「设备流程单」被归到硬件排查
    scores.sort(key=lambda item: (item[1], item[0].startswith("MES") or item[0] in {
        "报工异常", "流程单流转异常", "接口异常", "权限问题", "系统配置",
        "版本与补丁", "定时任务", "数据同步",
    }), reverse=True)
    return scores[0][0], [c for c, _ in scores]


def vendor_hint(question: str) -> str:
    """问题文本中出现的厂商名（如「华智刷屏乱剔」→ 华智）。

    用于把问题归入厂商系统类，并让检索优先命中该厂商的文档。
    """
    q = question.strip().lower()
    for term in VENDOR_TERMS:
        if term.lower() in q:
            return term
    return ""


def vendor_in_chunk(chunk, vendor: str) -> bool:
    """分块是否与厂商相关：文档名 / 章节标题 / 摘要 / 原文任一处含厂商词。"""
    return any(
        vendor in (field or "")
        for field in (chunk.doc_name, chunk.heading, chunk.summary, chunk.text)
    )


def conflict_risk(hits) -> bool:
    """多条记录来自不同文档且得分接近时，判定为需要现场核实。"""
    if len(hits) < 2:
        return False
    top = hits[0].score
    if top <= 0:
        return False
    docs = {h.chunk.doc_id for h in hits[:4] if h.score >= top * 0.85}
    return len(docs) >= 2


# ---------- 文档分类（入库时自动判定，落库用于列表分组与后续检索过滤）----------

DOC_CATEGORIES = ("MES系统", "厂商系统", "硬件问题", "网络问题", "流水线问题", "流程问题", "常识库")
DOC_OTHER = "其他"
DOC_CATEGORY_ORDER = (*DOC_CATEGORIES, DOC_OTHER)

# 常识库：导航栏独立入口上传的基础事实数据（网段规划、设备/电脑 IP、配置、MAC、使用人等），
# 问答检索不过滤分类，常识文档与其他记录一起参与 BM25 / 向量检索。
KNOWLEDGE_CATEGORY = "常识库"

# 文档级关键词。名称命中权重最高，其次章节标题，最后摘要。
DOC_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "MES系统": [
        "MES", "WMS", "ERP", "报工", "报数", "流程单", "派工", "条码", "工序", "班次",
        "物料", "BOM", "工艺路线", "生产计划", "排产", "看板", "过站", "接口",
        "数据库", "账号", "权限", "菜单", "系统配置", "日志", "定时任务", "数据同步",
        "报表", "审批流",
    ],
    "厂商系统": [
        "厂商", "厂家", "供应商", "设备商", "第三方", "上位机", "原厂", "远程支持",
        "售后服务", "代理", "供应商系统", "专有软件", "授权", "许可证", "厂商工程师",
        "外部系统",
    ],
    "硬件问题": [
        "硬件", "电气", "机械", "电机", "伺服", "变频器", "传感器", "光电", "编码器",
        "气缸", "轴承", "皮带", "链条", "减速机", "接触器", "继电器", "电源", "风扇",
        "相机", "镜头", "光源", "机械手", "机器人", "空压机", "泵", "阀门", "流量计",
        "异响", "振动", "过热", "漏油", "烧毁", "跳闸", "不通电", "急停", "报警",
        "停机", "更换", "备件", "维修", "贴标机", "灌装机", "包装机",
    ],
    "网络问题": [
        "网络", "网段", "监控网段", "子网", "IP", "IP地址", "VLAN", "网关", "子网掩码",
        "交换机", "路由", "防火墙", "网卡", "网线", "光纤", "光模块", "链路", "端口",
        "丢包", "Ping", "不通", "断网", "网络中断", "网络抖动", "DNS", "DHCP",
        "网络配置", "IP冲突", "网络故障", "交换机配置", "接入层", "汇聚层", "核心交换",
    ],
    "流水线问题": [
        "流水线", "产线", "生产线", "输送线", "输送带", "皮带线", "装配线", "线体",
        "工位", "节拍", "堵料", "卡料", "堆积", "缓存区", "上下料", "转运", "分拣",
        "线速", "转产", "停机待料",
    ],
    "流程问题": [
        "流程", "作业指导", "SOP", "规范", "制度", "审批", "交接班", "点检", "巡检",
        "保养计划", "培训", "职责", "记录填写", "上报", "验收", "标准化",
        "处置流程", "应急",
    ],
}


def classify_document(name: str, headings: list[str], summaries: list[str], summary: str = "") -> str:
    """按文档名、章节标题和摘要做规则归类；无任何命中时归为「其他」。

    权重：文档名 3、章节标题 2、各段摘要 1、全文摘要 1。同一关键词只在本字段计一次。
    """
    fields = [(name, 3.0), ("\n".join(headings), 2.0), ("\n".join(summaries), 1.0), (summary, 1.0)]
    scores: dict[str, float] = {category: 0.0 for category in DOC_CATEGORIES}
    for text, weight in fields:
        if not text:
            continue
        corpus = _upper_mes(text).upper()
        for category, words in DOC_CATEGORY_KEYWORDS.items():
            hits = sum(1 for word in words if word.upper() in corpus)
            scores[category] += hits * weight
    best = max(DOC_CATEGORIES, key=lambda category: (scores[category], -DOC_CATEGORIES.index(category)))
    return best if scores[best] > 0 else DOC_OTHER


# 后台新增的自定义问题类型（运行时从数据库注册；自动判定不会产出这些值）
_custom_categories: set[str] = set()


def register_custom_categories(names) -> None:
    """注册后台新增的自定义类型（模块加载 / 新增接口时调用）。"""
    for name in names or []:
        text = str(name).strip()
        if text:
            _custom_categories.add(text)


def unregister_custom_category(name: str) -> None:
    _custom_categories.discard(str(name or "").strip())


def all_doc_categories() -> list:
    """全部可选问题类型：内置固定分类 + 后台新增的自定义类型。"""
    return [*DOC_CATEGORY_ORDER, *sorted(_custom_categories - set(DOC_CATEGORY_ORDER))]


def normalize_doc_category(value: str) -> str:
    """把外部传入的分类名规范成固定取值；无法识别时返回「其他」。"""
    text = (value or "").strip()
    return text if (text in DOC_CATEGORY_ORDER or text in _custom_categories) else DOC_OTHER


def normalize_doc_categories(values) -> list:
    """规范一组人工选择的分类标签：去空、去重、限长；不强制白名单（自定义类型即时生效）。"""
    tags: list[str] = []
    for value in values or []:
        text = str(value or "").strip()[:30]
        if text and text not in tags:
            tags.append(text)
    return tags


def record_categories(record) -> list:
    """文档的全部分类标签：categories 优先，空时回退到主分类（自动判定结果）。"""
    tags = list(getattr(record, "categories", None) or [])
    if not tags:
        primary = normalize_doc_category(getattr(record, "category", "") or "其他")
        if primary:
            tags = [primary]
    return tags
