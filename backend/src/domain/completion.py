"""未测适配特征补全核算。

两厂交来的内衬特征中，未测项保留下来：每项可有可无，同一未测项的取值
在同一核算单元（指定打样组或切树候选组）内按规格共用。沿用归并切线，
对每个单元穷举未测项的取值，给出：

- always（无论怎么补都达标）：任意补法下组内全部规格对距离都不超过切线；
- partial（只有部分补法达标）：存在达标补法，也存在超线补法；
- over（怎么补都超线）：任意补法下至少有一个规格对严格超过切线。

距离比较全部使用 Fraction 精确分数，避免浮点误差。
"""

from fractions import Fraction
from itertools import combinations

PENDING_MARK = "?"
# 单组未测变量超过该上限时不再穷举，按证据不足处理，避免指数爆炸。
VARIABLE_CAP = 12

VERDICTS = ("always", "partial", "over")
VERDICT_LABELS = {
    "always": "无论怎么补都达标",
    "partial": "只有部分补法达标",
    "over": "怎么补都超线",
    "blocked": "未测项过多，已停止穷举",
}

_VERDICT_COLUMNS = (
    ("group", "核算单元"),
    ("source", "单元来源"),
    ("members", "规格编号"),
    ("measured", "已测特征数"),
    ("pending_variables", "未测变量数"),
    ("assignments", "补法总数"),
    ("verdict", "补全结论"),
    ("pass_count", "达标补法"),
    ("over_count", "超线补法"),
    ("pass_example", "达标补法示例"),
    ("over_example", "超线补法示例"),
)


def parse_record(record):
    """读取规范化后的台账行，兼容没有未测列的旧批次。

    未测项在入库校验时已去掉问号并按集合去重，这里直接读取名称集合。
    """
    measured = {item.strip() for item in record["fit_traits"].split("|") if item.strip()}
    pending = set()
    raw_pending = record.get("pending_traits")
    if isinstance(raw_pending, str) and raw_pending.strip():
        pending = {item.strip() for item in raw_pending.split("|") if item.strip()}
    return measured, pending


def _pair_counts(left, right, values):
    """给定未测变量取值，返回规格对的 (对称差计数, 并集计数)。"""
    left_measured, left_pending = left
    right_measured, right_pending = right
    left_set = set(left_measured)
    right_set = set(right_measured)
    for variable, present in values.items():
        if present:
            if variable in left_pending:
                left_set.add(variable)
            if variable in right_pending:
                right_set.add(variable)
    union = left_set | right_set
    symmetric = len(left_set) + len(right_set) - 2 * len(left_set & right_set)
    return symmetric, len(union)


def _is_over(symmetric, union, cut):
    """距离严格大于切线。cut 为 Fraction，全部按分数精确比较。"""
    return Fraction(symmetric, union) > cut


def _format_assignment(variables, values):
    if not variables:
        return "无未测项（已测特征即最终特征）"
    chosen = [f"{name}（补入）" for name in variables if values.get(name)]
    dropped = [f"{name}（确认不需要）" for name in variables if not values.get(name)]
    return "；".join(chosen + dropped)


def evaluate_group(cluster, records, cut):
    """核算一个单元，返回结论、计数和两种补法示例。

    cluster 为该单元在 records 中的下标元组，cut 为冻结的距离切线（0 至 1）。
    """
    parsed = [parse_record(records[index]) for index in cluster]
    variables = sorted({name for _, pending in parsed for name in pending})
    measured_union = sorted({name for measured, _ in parsed for name in measured})
    member_ids = "、".join(records[index]["spec_id"] for index in cluster)
    base = {
        "group": "",
        "source": "",
        "members": member_ids,
        "measured": len(measured_union),
        "pending_variables": len(variables),
        "assignments": 0,
        "verdict": "blocked",
        "pass_count": 0,
        "over_count": 0,
        "pass_example": "—",
        "over_example": "—",
    }
    if len(variables) > VARIABLE_CAP:
        base["verdict"] = "blocked"
        base["note"] = f"未测变量 {len(variables)} 个超过上限 {VARIABLE_CAP}，请拆分打样组或先补测。"
        return base, variables

    pairs = list(combinations(range(len(parsed)), 2))
    cut = Fraction(cut).limit_denominator(1_000_000)
    pass_count = over_count = 0
    pass_example = over_example = None
    total = 1 << len(variables)

    for mask in range(total):
        values = {name: bool((mask >> offset) & 1) for offset, name in enumerate(variables)}
        violated = False
        for left, right in pairs:
            symmetric, union = _pair_counts(parsed[left], parsed[right], values)
            if _is_over(symmetric, union, cut):
                violated = True
                break
        if violated:
            over_count += 1
            if over_example is None:
                over_example = _format_assignment(variables, values)
        else:
            pass_count += 1
            if pass_example is None:
                pass_example = _format_assignment(variables, values)

    if over_count == 0:
        verdict = "always"
    elif pass_count == 0:
        verdict = "over"
    else:
        verdict = "partial"
    base.update({
        "assignments": total,
        "verdict": verdict,
        "pass_count": pass_count,
        "over_count": over_count,
        "pass_example": pass_example if pass_example is not None else "—",
        "over_example": over_example if over_example is not None else "—",
    })
    return base, variables


def completion_table(rows):
    return {
        "title": "未测特征补全结论",
        "columns": [{"key": key, "label": label} for key, label in _VERDICT_COLUMNS],
        "rows": rows,
    }


def examples_table(rows):
    """展开每个单元的达标与超线补法，供采购逐条核对。"""
    example_rows = []
    for row in rows:
        example_rows.append({
            "group": row["group"], "members": row["members"], "kind": "达标补法",
            "verdict": VERDICT_LABELS[row["verdict"]], "assignment": row["pass_example"],
        })
        example_rows.append({
            "group": row["group"], "members": row["members"], "kind": "超线补法",
            "verdict": VERDICT_LABELS[row["verdict"]], "assignment": row["over_example"],
        })
    return {
        "title": "达标与超线补法示例",
        "columns": [
            {"key": "group", "label": "核算单元"}, {"key": "members", "label": "规格编号"},
            {"key": "kind", "label": "补法类型"}, {"key": "verdict", "label": "补全结论"},
            {"key": "assignment", "label": "取值示例"},
        ],
        "rows": example_rows,
    }
