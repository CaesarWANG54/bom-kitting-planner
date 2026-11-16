"""Local JSON, CSV and readable plan exports."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .errors import ValidationError


def _text(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def render_summary(outputs: dict[str, Any]) -> str:
    summary = outputs["run_summary"]
    plan = outputs["kitting_analysis"]
    draft = outputs["purchase_requisition_draft"]
    lines = ["# BOM 齐料计划", ""]
    if summary.get("synthetic_example"):
        lines += ["合成示例：主体、订单、价格和审批均为虚构数据。", ""]
    lines += [
        f"订单：{_text(plan['order_id'])} · 物料需求日：{plan['material_need_date']}",
        "",
        "| 物料 | 毛需求 | 可用库存 | 按期在途 | 已批准替代 | 剩余缺料 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in plan["lines"]:
        lines.append(
            "| "
            + " | ".join(
                _text(item[key])
                for key in (
                    "material_id",
                    "gross_requirement",
                    "effective_on_hand",
                    "timely_inbound",
                    "approved_substitution_offset",
                    "residual_shortage",
                )
            )
            + " |"
        )
    lines += ["", "## 采购申请草稿", ""]
    if draft["lines"]:
        lines += [
            "| 物料 | 建议数量 | 供应商建议 | 估算金额（CNY） | 复核要求 |",
            "|---|---:|---|---:|---|",
        ]
        for item in draft["lines"]:
            amount = item["estimated_line_amount_cny"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        _text(item["material_id"]),
                        str(item["suggested_purchase_qty"]),
                        _text(item["recommended_supplier_id"] or "待询价"),
                        f"{amount:.2f}" if amount is not None else "待询价",
                        "采购经理、财务审批" if item["major_purchase"] else "人工复核",
                    ]
                )
                + " |"
            )
    else:
        lines.append("当前输入下无剩余缺料。")
    lines += ["", "建议数量包含最小起订量和包装倍数；草稿尚未形成采购订单。", ""]
    return "\n".join(lines)


def _csv(rows: list[dict[str, Any]], fields: list[str]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        values = {key: row.get(key, "") for key in fields}
        for key, value in values.items():
            if isinstance(value, str) and value.lstrip().startswith(
                ("=", "+", "-", "@")
            ):
                values[key] = "'" + value
        writer.writerow(values)
    return stream.getvalue()


def write_outputs(outputs: dict[str, Any], destination: str | Path) -> Path:
    """Publish a complete bundle to a new directory; never overwrite a prior plan."""
    target = Path(destination).resolve()
    if target.exists():
        raise ValidationError("输出目录已存在，请为本次计划选择新目录")
    documents = {
        f"{name}.json": json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
        for name, value in outputs.items()
    }
    documents["summary.md"] = render_summary(outputs)
    documents["materials.csv"] = _csv(
        outputs["kitting_analysis"]["lines"],
        [
            "material_id",
            "material_name",
            "uom",
            "gross_requirement",
            "effective_on_hand",
            "timely_inbound",
            "late_inbound",
            "approved_substitution_offset",
            "residual_shortage",
            "kitting_status",
        ],
    )
    documents["purchase-requisition.csv"] = _csv(
        outputs["purchase_requisition_draft"]["lines"],
        [
            "material_id",
            "material_name",
            "uom",
            "source_shortage_qty",
            "suggested_purchase_qty",
            "overbuy_qty",
            "recommended_supplier_id",
            "estimated_line_amount_cny",
            "approval_status",
        ],
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".bom-plan-", dir=target.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        for name, content in documents.items():
            (bundle / name).write_text(content, encoding="utf-8", newline="\n")
        # Reserve the final name so a concurrent run cannot replace an existing plan.
        target.mkdir()
        for child in bundle.iterdir():
            child.rename(target / child.name)
    return target
