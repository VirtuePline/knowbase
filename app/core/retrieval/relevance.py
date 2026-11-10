"""保守的故障意图校验：比较标题和明确现象，不把处理/原因中的偶然提词当成故障。"""
from __future__ import annotations

import re

from app.domain.schemas import ScoredChunk

_PROBLEM_FIELDS = {"故障现象", "问题描述", "问题现象", "故障描述", "故障问题", "现象", "问题"}
_DEVICE_FIELDS = {"设备", "部件", "设备/部件", "设备名称", "设备型号", "设备编号", "故障设备",
                  "系统", "系统名称", "系统/设备", "系统/设备型号", "系统设备型号", "模块"}
_HEADING = re.compile(r"^\s*(#{1,6})\s*(.+?)\s*#*\s*$")
_FIELD = re.compile(r"^\s*(?:[-*+]\s+|\d+[.、]\s*)?(?:\*\*|__)?"
                    r"(?P<label>[^:：\n]{1,96}?)(?:\*\*|__)?\s*[:：]\s*(?P<body>.*)$")
_BREAK = re.compile(r"<br\s*/?>", re.I)
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_REPORT_QUERY = re.compile(r"统计|汇总|清单|列表|数量|多少(?:条|个|次|份|台)")

# 每组只识别明确的现象表达，不以“电脑”“设备”等对象词代替故障。
_SYMPTOMS = {
    "startup": re.compile(r"开不了机|开不起来|无法开机|不能开机|不开机|无法启动|不能启动|启动不了|"
                          r"启动失败|无法上电|不能上电|不通电|无法通电|通不了电|不能通电|开机失败|开机无反应"),
    "audio": re.compile(r"(?:没(?:有)?|无|不出|没有了)(?:声音|声|音频)|(?:声音|音频)(?:没(?:有)?|消失|异常|太小)|"
                        r"听不到|听不见|不响|无声|静音|杂音|爆音|破音"),
    "printing": re.compile(r"无法打印|不能打印|打印不了|不打印|打印失败|打印不出|卡纸|打印乱码|缺墨|打印模糊"),
    "network": re.compile(r"断网|掉线|丢包|网络不通|网络中断|网络断开|无法联网|不能联网|连不上网|上不了网|"
                          r"无法连接|连接失败|连接超时|通信失败|通讯失败|通信中断|通讯中断|网口不通|网线松"),
    "access": re.compile(r"(?:网页|页面|网站|网址|浏览器|链接|软件|客户端|系统|应用|程序|界面|文件夹?|菜单)"
                         r".{0,10}(?:打不开|无法打开|不能打开|访问不了|无法访问|不能访问|进不去|启动失败|无法启动|启动不了)|"
                         r"(?:打不开|无法打开|不能打开|访问不了|无法访问|不能访问|进不去).{0,5}"
                         r"(?:网页|页面|网站|网址|浏览器|链接|软件|客户端|系统|应用|程序|界面|文件夹?|菜单)|"
                         r"访问失败|页面报错|网页报错"),
    "login": re.compile(r"无法登录|不能登录|登录不了|登录失败|登不进去|账号锁定|账户锁定|密码错误|账号或密码错误"),
    "crash": re.compile(r"闪退|崩溃|死机|卡死|无响应|程序退出|软件退出"),
    "display": re.compile(r"黑屏|花屏|蓝屏|屏幕不亮|显示器不亮|无显示|屏幕闪烁|图像发虚|图像模糊"),
    "mechanical": re.compile(r"异响|振动|卡料|堵料|漏油|漏气|过热|温度过高|不转|不动作"),
}
_DEVICES = {
    "computer": re.compile(r"电脑|计算机|工控机|笔记本|工作站|电脑主机|工控主机|\bpc\b", re.I),
    "speaker": re.compile(r"音响|音箱|喇叭|扬声器|耳机|声卡"),
    "printer": re.compile(r"打印机|条码打印|标签打印|印刷机"),
    "network_device": re.compile(r"交换机|路由器|网关|防火墙"),
    "camera": re.compile(r"相机|摄像头|视觉检测"),
    "scanner": re.compile(r"扫码枪|扫描枪|扫码器|读码器"),
    "motor": re.compile(r"电机|马达"),
    "labeler": re.compile(r"贴标机"),
    "filler": re.compile(r"灌装机"),
    "display": re.compile(r"显示器|屏幕"),
    "mes": re.compile(r"(?<![a-z])mes(?![a-z])", re.I),
    "wms": re.compile(r"(?<![a-z])wms(?![a-z])", re.I),
    "erp": re.compile(r"(?<![a-z])erp(?![a-z])", re.I),
}
_ACCESS_OBJECT = re.compile(r"网页|页面|网站|网址|浏览器|链接|软件|客户端|系统|应用|程序|界面|文件|菜单")
_PHYSICAL_DEVICES = {"computer", "speaker", "printer", "network_device", "camera", "scanner", "motor",
                     "labeler", "filler", "display"}


def normalize_fault_label(value: str) -> str:
    """去除列表标记和括号说明，保留字段本义（如“现象（遇到什么问题）”）。"""
    value = re.sub(r"^\s*(?:[-*+]\s+|\d+[.、]\s*)", "", value)
    value = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", "", value)
    return re.sub(r"[\s*`_#：:（）()\[\]【】]", "", value)


def _device_groups(text: str) -> set[str]:
    return {group for group, pattern in _DEVICES.items() if pattern.search(text)}


def _symptom_groups(text: str) -> set[str]:
    groups = {group for group, pattern in _SYMPTOMS.items() if pattern.search(text)}
    # “程序无法启动”是应用访问问题；“电脑打不开”在没有网页/应用限定时指向启动。
    if "access" in groups and not re.search(r"开不了机|无法开机|不能开机|不开机|不通电|无法上电", text):
        groups.discard("startup")
    if ("打不开" in text and not _ACCESS_OBJECT.search(text)
            and _device_groups(text) & _PHYSICAL_DEVICES):
        groups.add("startup")
    return groups


def _symptoms_compatible(requested: set[str], observed: set[str]) -> bool:
    if requested & observed:
        return True
    # 网络中断可能表现为网页无法访问；显示故障也可能表现为无法正常启动。
    compatible = ({"network", "access"}, {"startup", "display"})
    return any(requested & pair and observed & pair for pair in compatible)


def fault_matches(question: str, *, title: str = "", problem: str = "", device: str = "") -> bool:
    """仅明确冲突时返回 False；原因和处理步骤不应传入 problem/device。"""
    if not question.strip() or _REPORT_QUERY.search(question):
        return True
    requested = _symptom_groups(question)
    if not requested:
        return True
    # 明确现象字段优先于概括性文档标题，特别是包含多条故障的维修台账。
    observed = _symptom_groups(problem) or _symptom_groups(title)
    if observed and not _symptoms_compatible(requested, observed):
        return False
    requested_devices = _device_groups(question)
    observed_devices = _device_groups(f"{device}\n{problem}") or _device_groups(title)
    if requested_devices and observed_devices and not requested_devices & observed_devices:
        # 电脑音频/显示问题可以由相应外设记录解释；其他设备不能仅靠相同现象互相套用。
        family_pairs = (({"computer", "speaker"}, "audio"), ({"computer", "display"}, "display"))
        if not any(requested_devices & family and observed_devices & family
                   and symptom in requested and symptom in observed for family, symptom in family_pairs):
            return False
    return True


def _table_subjects(rows: list[list[str]]) -> list[dict[str, str]]:
    if not rows:
        return []
    headers = [normalize_fault_label(str(cell)) for cell in rows[0]]
    columns = [index for index, header in enumerate(headers) if header in _PROBLEM_FIELDS | _DEVICE_FIELDS]
    if not columns:
        # 纵向“字段 | 内容”表格。
        subject: dict[str, str] = {}
        for row in rows:
            if len(row) < 2:
                continue
            label = normalize_fault_label(str(row[0]))
            if label in _PROBLEM_FIELDS | _DEVICE_FIELDS:
                key = "problem" if label in _PROBLEM_FIELDS else "device"
                subject[key] = subject.get(key, "") + "\n" + str(row[1])
        return [subject] if subject else []
    subjects = []
    for row in rows[1:]:
        subject = {}
        for index in columns:
            if index < len(row) and str(row[index]).strip():
                key = "problem" if headers[index] in _PROBLEM_FIELDS else "device"
                subject[key] = subject.get(key, "") + "\n" + _BREAK.sub("\n", str(row[index]))
        if subject:
            subjects.append(subject)
    return subjects


def _markdown_tables(text: str) -> list[list[list[str]]]:
    tables = []
    current: list[list[str]] = []
    for line in [*text.splitlines(), ""]:
        if "|" in line:
            cells = [cell.strip().replace(r"\|", "|")
                     for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if len(cells) >= 2:
                current.append(cells)
                continue
        if len(current) >= 2 and all(_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in current[1]):
            tables.append([current[0], *current[2:]])
        current = []
    return tables


def _text_subjects(text: str) -> list[dict[str, str]]:
    # 只收集显式设备/现象字段，遇到处理、结果、原因等其他字段立即结束。
    subjects = []
    subject: dict[str, str] = {}
    active = ""
    text = re.sub(r"[；;]\s*(?=(?:\*\*)?[^:：\n；;]{1,96}[:：])", "\n", text)
    for line in text.splitlines():
        heading = _HEADING.match(line)
        field = _FIELD.match(line) if not heading else None
        if not heading and not field:
            if active and "|" not in line:
                subject[active] += "\n" + line
            continue
        label = normalize_fault_label(heading[2] if heading else field["label"])
        kind = "problem" if label in _PROBLEM_FIELDS else "device" if label in _DEVICE_FIELDS else ""
        if kind:
            if kind in subject and subject[kind].strip():
                subjects.append(subject)
                subject = {}
            active = kind
            subject[kind] = (field["body"] if field else "").strip("* ")
        else:
            active = ""
            if subject:
                subjects.append(subject)
                subject = {}
    if subject:
        subjects.append(subject)
    return subjects


def filter_fault_hits(question: str, hits: list[ScoredChunk]) -> list[ScoredChunk]:
    """保留故障意图不冲突的命中；多行表中存在兼容行即保留整个命中。"""
    if not question.strip() or _REPORT_QUERY.search(question) or not _symptom_groups(question):
        return list(hits)
    filtered = []
    for hit in hits:
        chunk = hit.chunk
        title = f"{chunk.doc_name}\n{chunk.heading}"
        first_line = next((line.strip() for line in chunk.text.splitlines() if line.strip()), "")
        if len(first_line) < 80 and "|" not in first_line and not _FIELD.match(first_line):
            title += "\n" + first_line
        subjects = _table_subjects(chunk.table) if chunk.table else []
        for table in _markdown_tables(chunk.text):
            subjects.extend(_table_subjects(table))
        subjects.extend(_text_subjects(chunk.text))
        explicit_problems = [subject for subject in subjects if subject.get("problem", "").strip()]
        if explicit_problems:
            subjects = explicit_problems
        if (any(fault_matches(question, title=title, **subject) for subject in subjects)
                if subjects else fault_matches(question, title=title)):
            filtered.append(hit)
    return filtered
