"""按必需适配特征执行完全链接归并，并对切树候选组核算未测项补全。

计算分两段，便于中断恢复：

1. ``run_clustering`` 为纯函数，产出切树候选组、归并轨迹和超线证据；
2. ``analyze_rows`` 在候选组上逐组执行未测特征补全核算，每组完成后即可
   落库为检查点，重试时从检查点继续，不重复核算已完成的组。
"""

from array import array
from dataclasses import dataclass
from heapq import heapify, heappop, heappush, heapreplace
from hashlib import sha256
from itertools import product
from json import dumps

from . import completion
from .spec import SPEC


_PAIR_LIMIT = 50
_CUT = SPEC["params"][0]
_PAIR_COLUMNS = (
    ("left_id", "左侧规格"), ("right_id", "右侧规格"), ("distance", "Jaccard 距离"),
    ("left_only", "左侧独有特征"), ("right_only", "右侧独有特征"),
)


@dataclass(frozen=True)
class _Cluster:
    node: str
    members: tuple[int, ...]
    height: float = 0.0


def validate_rows(rows):
    """校验 CSV 规格行，返回可再次导入的规范化副本。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("内衬明细为空，请导入至少一条内衬规格。")
    normalized = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条内衬规格格式有误，请核对样例 CSV。")
        record = {}
        for field in SPEC["fields"]:
            value = row.get(field["key"])
            if field.get("required", True):
                if not isinstance(value, str) or not 0 < len(value.strip()) <= field["max_length"]:
                    raise ValueError(f"第 {index} 条的「{field['label']}」须为 1 至 {field['max_length']} 个字符。")
                record[field["key"]] = value.strip()
            else:
                if value is None:
                    record[field["key"]] = ""
                elif not isinstance(value, str) or len(value.strip()) > field["max_length"]:
                    raise ValueError(f"第 {index} 条的「{field['label']}」须为至多 {field['max_length']} 个字符。")
                else:
                    record[field["key"]] = value.strip()
        spec_id = record["spec_id"].casefold()
        if spec_id in seen:
            raise ValueError(f"第 {index} 条的规格编号「{record['spec_id']}」重复，请保留唯一规格。")
        seen.add(spec_id)
        traits = {trait.strip() for trait in record["fit_traits"].split("|")}
        if "" in traits:
            raise ValueError(f"规格「{record['spec_id']}」含空特征，请检查 | 分隔符两侧。")
        record["fit_traits"] = "|".join(sorted(traits))
        pending = set()
        pending_cell = record.get("pending_traits") or ""
        if pending_cell:
            for raw in pending_cell.split("|"):
                name = raw.strip()
                if not name:
                    raise ValueError(f"规格「{record['spec_id']}」的未测特征含空项，请检查 | 分隔符两侧。")
                if not name.endswith(completion.PENDING_MARK):
                    raise ValueError(
                        f"规格「{record['spec_id']}」的未测特征「{name}」须以问号结尾，已测特征请放入必需特征列。"
                    )
                bare = name[:-1].strip()
                if not bare:
                    raise ValueError(f"规格「{record['spec_id']}」存在只有问号的未测特征，请补全特征名称。")
                if bare in traits:
                    raise ValueError(f"规格「{record['spec_id']}」的特征「{bare}」已测，请勿再标为未测。")
                pending.add(bare)
        record["pending_traits"] = "|".join(sorted(pending))
        group_code = (record.get("group_code") or "").strip()
        if len(group_code) > 40:
            raise ValueError(f"规格「{record['spec_id']}」的打样组编号须为至多 40 个字符。")
        record["group_code"] = group_code
        normalized.append(record)
    return normalized


def _cut_distance(params):
    if not isinstance(params, dict) or params.keys() - {_CUT["key"]}:
        raise ValueError("归并参数格式有误，请检查归并距离切线。")
    cut = params.get(_CUT["key"], _CUT["default"])
    if type(cut) not in (int, float) or not _CUT["min"] <= cut <= _CUT["max"]:
        raise ValueError("归并距离切线须为 0 至 1 的数值。")
    return cut


def _jaccard(left, right):
    shared = len(left & right)
    union = len(left) + len(right) - shared
    return (union - shared) / union


def _pair_distances(traits, cut):
    """建立下三角距离矩阵，同时保留最远的超线规格对。"""
    matrix = []
    incompatible = []
    count = 0
    for right, right_traits in enumerate(traits):
        distances = array("d")
        for left in range(right):
            distance = _jaccard(traits[left], right_traits)
            distances.append(distance)
            if distance > cut:
                count += 1
                priority = (distance, -left, -right)
                if len(incompatible) < _PAIR_LIMIT:
                    heappush(incompatible, priority)
                elif priority > incompatible[0]:
                    heapreplace(incompatible, priority)
        matrix.append(distances)
    return matrix, count, [(-left, -right, height) for height, left, right in sorted(incompatible, reverse=True)]


def _traits_text(traits):
    return " | ".join(sorted(traits)) or "—"


def _pair_row(ids, traits, left, right, distance):
    return {
        "left_id": ids[left], "right_id": ids[right], "distance": distance,
        "left_only": _traits_text(traits[left] - traits[right]),
        "right_only": _traits_text(traits[right] - traits[left]),
    }


def _hierarchy(matrix, ids, traits, cut):
    active = {i: _Cluster(f"规格 {spec_id}", (i,)) for i, spec_id in enumerate(ids)}
    nearest = [(-1.0, i, i) for i in range(len(ids) - 1)]
    heapify(nearest)
    cut_clusters = None
    merges = []
    while len(active) > 1:
        height, left, right = heappop(nearest)
        if left not in active:
            continue
        # A slot stays with its smallest leaf. Distances can only increase, so stale
        # heap entries are lower bounds. Refresh a row only when it reaches the top.
        if right not in active or right == left or matrix[right][left] != height:
            best = min(((matrix[j][left], j) for j in active if j > left), default=None)
            if best is not None:
                heappush(nearest, (best[0], left, best[1]))
            continue

        if height > cut and cut_clusters is None:
            cut_clusters = list(active.values())
        left_cluster, right_cluster = active.pop(right), active[left]
        witness_left, witness_right = next(
            (a, b) for a, b in product(left_cluster.members, right_cluster.members)
            if _jaccard(traits[a], traits[b]) == height
        )
        step = len(merges) + 1
        parent = _Cluster(
            f"归并 {step}", tuple(sorted(left_cluster.members + right_cluster.members)), height,
        )
        merges.append({
            "step": step, "left_node": left_cluster.node, "right_node": right_cluster.node,
            "node": parent.node, "size": len(parent.members), "retained": "保留" if height <= cut else "切开",
            **_pair_row(ids, traits, witness_left, witness_right, height),
        })
        active[left] = parent
        for other in active:
            if other != left:
                target = matrix[max(left, other)]
                source = matrix[max(right, other)]
                target_index = min(left, other)
                target[target_index] = max(target[target_index], source[min(right, other)])
        heappush(nearest, (height, left, right))
    return (list(active.values()) if cut_clusters is None else cut_clusters), merges


def _cluster_row(index, cluster, ids, names, traits):
    member_traits = [traits[i] for i in cluster.members]
    shared = set.intersection(*member_traits)
    return {
        "group": f"G{index:02d}", "node": cluster.node,
        "size": len(cluster.members), "diameter": cluster.height,
        "members": "、".join(ids[i] for i in cluster.members),
        "names": "、".join(names[i] for i in cluster.members),
        "shared": _traits_text(shared), "variable": _traits_text(set.union(*member_traits) - shared),
    }


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def frozen_signature(records, cut):
    """冻结批次、组员与切线后的检查点指纹。"""
    payload = dumps({"rows": records, "cut": cut}, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def run_clustering(records, cut):
    """纯函数：只做完全链接归并与切树，不触碰未测项补全。"""
    ids = [row["spec_id"] for row in records]
    names = [row["name"] for row in records]
    traits = [set(row["fit_traits"].split("|")) for row in records]
    matrix, pair_count, incompatible = _pair_distances(traits, cut)
    clusters, merges = _hierarchy(matrix, ids, traits, cut)
    groups = [_cluster_row(index, cluster, ids, names, traits) for index, cluster in enumerate(clusters, start=1)]
    pairs = [_pair_row(ids, traits, left, right, height) for left, right, height in incompatible]
    return clusters, groups, merges, pairs, pair_count


def _pending_text(records, cluster):
    pending = sorted({
        f"{name}{completion.PENDING_MARK}"
        for index in cluster
        for name in completion.parse_record(records[index])[1]
    })
    return " | ".join(pending) or "—"


def _build_sampling_units(records, clusters):
    """返回核算单元列表：(单元编号, 来源, 规格下标元组)。

    台账里带打样组编号的规格按编号聚合成指定打样组（可跨切树候选组）；
    其余规格沿用切树候选组，编号 G01、G02……，指定组用其编号。
    """
    designated = {}
    for index, record in enumerate(records):
        code = record.get("group_code") or ""
        if code:
            designated.setdefault(code, []).append(index)
    assigned = {index for indices in designated.values() for index in indices}
    units = []
    for code in sorted(designated, key=lambda value: value.casefold()):
        units.append((code, "指定打样组", tuple(sorted(designated[code]))))
    for index, cluster in enumerate(clusters, start=1):
        leftover = tuple(member for member in cluster.members if member not in assigned)
        if leftover:
            units.append((f"G{index:02d}", "切树候选组", leftover))
    return units


def stream_analysis(rows, normalized_params, progress=None):
    """逐单元产出检查点的生成器，供 worker 中断恢复。

    每完成一个核算单元 yield 一次可落库的 progress；全部完成后最后一次 yield
    (result, progress)。重试时传入上次落库的 progress 即可跳过已完成的单元，
    不会重复核算或重复发布。
    """
    records = sorted(validate_rows(rows), key=lambda row: row["spec_id"].casefold())
    cut = _cut_distance(normalized_params)
    signature = frozen_signature(records, cut)
    if progress and progress.get("signature") != signature:
        progress = None
    clusters, groups, merges, pairs, pair_count = run_clustering(records, cut)
    units = _build_sampling_units(records, clusters)

    done = {}
    if progress:
        done = {item["index"]: item for item in progress.get("groups", []) if item.get("done")}
    completion_rows = []
    checkpoints = []
    for ordinal, (unit_code, source, members) in enumerate(units, start=1):
        if ordinal in done:
            row = dict(done[ordinal]["row"])
        else:
            row, _variables = completion.evaluate_group(members, records, cut)
            row["group"] = unit_code
            row["source"] = source
            checkpoints.append({"index": ordinal, "code": unit_code, "done": True, "row": dict(row)})
            snapshot = {
                "signature": signature,
                "cut": cut,
                "total_groups": len(units),
                "groups": sorted([*done.values(), *checkpoints], key=lambda item: item["index"]),
            }
            yield None, snapshot
        completion_rows.append(row)

    assigned_codes = {record.get("group_code") for record in records if record.get("group_code")}
    for index, cluster in enumerate(clusters, start=1):
        groups[index - 1]["pending"] = _pending_text(records, cluster.members)
        groups[index - 1]["sample_group"] = "、".join(
            sorted(code for code in assigned_codes
                   if any(records[member].get("group_code") == code for member in cluster.members))
        ) or "—"

    kept = len(records) - len(groups)
    note = f"距离切线为 {cut}，含等于切线的归并。未测项保留并按组穷举补法，同一规格的取值在组内各对共用。"
    if pair_count > len(pairs):
        note += f"超线清单展示距离最高的 {len(pairs)} 对，共有 {pair_count} 对超线。"
    rerun = "按候选组核对共有特征、规格间差异和未测项补全结论，记录实物打样结论。"
    if kept < len(merges):
        boundary = merges[kept]
        rerun = (
            f"第一处切开出现在第 {boundary['step']} 步，"
            f"{boundary['left_id']} 与 {boundary['right_id']} 的距离为 {boundary['distance']:.6g}。"
            "核对两侧独有特征，修订台账或切线后可重跑。"
        )

    verdict_counts = {verdict: sum(row["verdict"] == verdict for row in completion_rows)
                      for verdict in (*completion.VERDICTS, "blocked")}
    pending_specs = sum(bool(record.get("pending_traits")) for record in records)
    designated_count = len({record.get("group_code") for record in records if record.get("group_code")})
    headline = (
        f"{len(records)} 条内衬规格切出 {len(groups)} 个候选组，另含 {designated_count} 个指定打样组；"
        f"{verdict_counts['always']} 个单元无论怎么补都达标、{verdict_counts['partial']} 个单元部分补法达标、"
        f"{verdict_counts['over']} 个单元怎么补都超线。"
    )
    final_progress = {
        "signature": signature,
        "cut": cut,
        "total_groups": len(units),
        "groups": sorted([*done.values(), *checkpoints], key=lambda item: item["index"]),
    }

    verdict_section = (
        "补全核算在冻结批次、组员与切线后逐单元进行：未测特征每项可有可无，"
        "同一规格的取值在组内各规格对间共用。指定打样组按台账编号聚合，可跨切树候选组；"
        "未指定的规格沿用切树候选组。结论为「无论怎么补都达标」的单元可直接安排共用打样；"
        "「只有部分补法达标」的单元按达标示例安排补测并复核；「怎么补都超线」的单元应保留独立规格。"
    )
    result = {
        "state": "warning" if pair_count or verdict_counts["partial"] or verdict_counts["over"] else "ok",
        "headline": headline,
        "note": note,
        "metrics": [
            {"label": "内衬规格", "value": len(records), "detail": "纳入归并的有效台账条目"},
            {"label": "切树候选组", "value": len(groups), "detail": f"其中 {sum(group['size'] == 1 for group in groups)} 组仅含一条规格"},
            {"label": "指定打样组", "value": designated_count, "detail": "采购指定核算能否共用的组"},
            {"label": "含未测项规格", "value": pending_specs, "detail": "保留两厂尚未测完的特征"},
            {"label": "无论怎么补都达标", "value": verdict_counts["always"], "detail": "任意补法下组内规格对均不超线"},
            {"label": "部分补法达标", "value": verdict_counts["partial"], "detail": "达标与超线补法都存在"},
            {"label": "怎么补都超线", "value": verdict_counts["over"], "detail": "未测项无法消除已测差异"},
            {"label": "保留归并", "value": kept, "detail": f"完整归并树共有 {len(merges)} 步"},
            {"label": "超线组合", "value": pair_count, "detail": "距离严格大于切线的规格对"},
        ],
        "tables": [
            _table("切树候选组", (
                ("group", "候选组"), ("node", "树节点"), ("size", "规格数"), ("diameter", "组内最大距离"),
                ("members", "规格编号"), ("names", "内衬名称"), ("shared", "共有特征"),
                ("variable", "差异特征"), ("pending", "未测特征"), ("sample_group", "指定打样组"),
            ), groups),
            completion.completion_table(completion_rows),
            completion.examples_table(completion_rows),
            _table("完全链接归并轨迹", (
                ("step", "步骤"), ("left_node", "左子节点"), ("right_node", "右子节点"),
                ("node", "归并节点"), ("size", "归并后规格数"), ("retained", "切树判定"),
            ) + _PAIR_COLUMNS, merges),
            _table("超线规格组合", _PAIR_COLUMNS, pairs),
        ],
        "charts": [{
            "title": "完全链接归并高度", "kind": "line",
            "series": [{"label": f"第 {row['step']} 步", "value": row["distance"]} for row in merges],
        }],
        "sections": [
            {"title": "未测项补全口径", "text": verdict_section},
            {"title": "切树复核与重跑", "text": rerun},
        ],
        "progress": {"done": len(final_progress["groups"]), "total": len(units), "signature": signature},
    }
    yield result, final_progress


def analyze_rows(rows, normalized_params, progress=None):
    """归并并逐组核算未测项补全，支持检查点恢复。

    progress 为 None 时全新核算；否则需带同一冻结指纹，已完成的候选组直接
    复用检查点，只补算剩余组。返回 (result, progress)，progress 可随时落库。
    """
    result = final_progress = None
    for result, final_progress in stream_analysis(rows, normalized_params, progress):
        pass
    return result, final_progress


def analyze(rows, normalized_params):
    """无检查点的兼容入口，返回框架通用结果。"""
    result, _progress = analyze_rows(rows, normalized_params)
    return result
