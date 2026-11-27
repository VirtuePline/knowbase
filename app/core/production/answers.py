"""将已按当前账号权限汇总的报表行呈现为本地问答。"""
from datetime import timedelta

from app.core.production.intent import ProductionQuery
from app.core.production.store import ProductionResult
from app.domain.schemas import AnswerResponse


_METRICS = {"output": "产量", "good": "良品数", "defect": "不良品数", "yield_rate": "良率", "order_count": "工单数"}
_DIMENSIONS = {"line": "产线", "product": "产品", "shift": "班次", "day": "日期", "order_id": "工单"}


def production_response(status: str, text: str) -> AnswerResponse:
    return AnswerResponse(status=status, answer=text, answer_origin="production_sql", category="生产数据")


def render_report(plan: ProductionQuery, result: ProductionResult) -> AnswerResponse:
    last = plan.end - timedelta(days=1)
    period = str(plan.start) if last == plan.start else f"{plan.start} 至 {last}"
    lines = [f"统计日期 {period}（当前账号可查部门）"]
    if plan.filters:
        lines.append("筛选条件 " + "，".join(f"{_DIMENSIONS[key]}={value}" for key, value in plan.filters))
    if not result.rows:
        lines.append("此范围内没有生产记录。")
        return production_response("not_found", "\n".join(lines))
    for row in result.rows:
        fields = []
        if plan.group_by:
            value = row[plan.group_by]
            fields.append(f"{_DIMENSIONS[plan.group_by]} {value if value is not None else '未填写'}")
        for metric in plan.metrics:
            value = row[metric]
            if value is None:
                display = "无法计算"
            elif metric == "yield_rate":
                display = f"{value:.2f}%"
            else:
                display = f"{value:,}" + (" 单" if metric == "order_count" else " 件")
            fields.append(f"{_METRICS[metric]} {display}")
        lines.append("，".join(fields))
    if result.truncated:
        lines.append(f"仅显示前 {len(result.rows)} 组，可缩小统计范围。")
    return production_response("ok", "\n".join(lines))
