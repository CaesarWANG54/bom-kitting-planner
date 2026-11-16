"""BOM and kitting planning with the Python standard library."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    BomCycleError,
    ProhibitedAutomationError,
    UnsafeConfigurationError,
    ValidationError,
)


PROHIBITED_COMMANDS = frozenset(
    {
        "create_purchase_order",
        "release_purchase_order",
        "send_purchase_order",
        "auto_approve_substitution",
        "auto_approve_major_purchase",
        "release_iqc_inventory",
    }
)

PROTECTED_INVARIANTS = frozenset(
    {
        "auto_create_purchase_order",
        "auto_release_purchase_order",
        "auto_send_purchase_order",
        "auto_approve_substitution",
        "auto_approve_major_purchase",
        "auto_release_iqc_inventory",
    }
)

SCENARIO_FILES = {
    "order": "order.json",
    "materials": "materials.json",
    "bom_items": "bom_items.json",
    "inventory_positions": "inventory_positions.json",
    "inbound_supply": "inbound_supply.json",
    "substitution_rules": "substitution_rules.json",
    "suppliers": "suppliers.json",
    "supplier_quotes": "supplier_quotes.json",
    "approval_decisions": "approval_decisions.json",
}


def load_json(path: str | Path) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError("JSON 字段重复", details={"field": key})
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValidationError("JSON 包含非有限数字", details={"value": value})

    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(
            handle, object_pairs_hook=pairs, parse_constant=invalid_constant
        )


def load_scenario(directory: str | Path) -> dict[str, Any]:
    base = Path(directory)
    if base.is_file():
        value = load_json(base)
        if not isinstance(value, dict):
            raise ValidationError("scenario 必须是 JSON 对象")
        return value
    return {
        name: load_json(base / filename) for name, filename in SCENARIO_FILES.items()
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(
            f"{field} 不能是布尔值", details={"field": field, "value": value}
        )
    try:
        number = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal error types vary by input
        raise ValidationError(
            f"{field} 不是有效数字", details={"field": field, "value": value}
        ) from exc
    if not number.is_finite():
        raise ValidationError(
            f"{field} 必须是有限数字", details={"field": field, "value": value}
        )
    return number


def _non_negative(value: Any, field: str) -> Decimal:
    number = _decimal(value, field)
    if number < 0:
        raise ValidationError(
            f"{field} 不得为负数", details={"field": field, "value": value}
        )
    return number


def _positive(value: Any, field: str) -> Decimal:
    number = _decimal(value, field)
    if number <= 0:
        raise ValidationError(
            f"{field} 必须大于 0", details={"field": field, "value": value}
        )
    return number


def _whole_quantity(value: Any, field: str) -> Decimal:
    number = _non_negative(value, field)
    if number != number.to_integral_value():
        raise ValidationError(f"{field} 必须为 PCS 整数数量")
    return number


def _to_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"{field} 必须是 ISO 日期", details={"field": field, "value": value}
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            f"{field} 不是有效 ISO 日期", details={"field": field, "value": value}
        ) from exc


def _to_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"{field} 必须是 ISO 时间", details={"field": field, "value": value}
        )
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            f"{field} 不是有效 ISO 时间", details={"field": field, "value": value}
        ) from exc
    if result.tzinfo is None:
        raise ValidationError(
            f"{field} 必须包含时区", details={"field": field, "value": value}
        )
    return result


def _number(value: Decimal | int | float) -> int | float:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if number == number.to_integral_value():
        return int(number)
    return float(number.quantize(Decimal("0.000001")))


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _floor(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _fields(value: Any, required: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} 必须是对象")
    missing = sorted(key for key in required if value.get(key) is None)
    if missing:
        raise ValidationError(f"{label} 字段缺失", details={"fields": missing})


def _unique_index(
    rows: Iterable[dict[str, Any]], key: str, dataset: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value:
            raise ValidationError(
                f"{dataset}.{key} 缺失", details={"dataset": dataset, "row": row}
            )
        if value in result:
            raise ValidationError(
                f"{dataset}.{key} 重复",
                details={"dataset": dataset, "field": key, "value": value},
            )
        result[value] = row
    return result


class _AuditChain:
    def __init__(
        self,
        generated_at: str,
        correlation_id: str,
        handoff_context: dict[str, Any] | None = None,
    ) -> None:
        self._base = _to_datetime(generated_at, "runtime.generated_at")
        self._correlation_id = correlation_id
        self._handoff_context = copy.deepcopy(handoff_context or {})
        self.events: list[dict[str, Any]] = []

    def add(self, event_type: str, payload: dict[str, Any]) -> None:
        sequence = len(self.events) + 1
        event = {
            "event_id": f"EVT-{self._correlation_id}-{sequence:04d}",
            "sequence": sequence,
            "occurred_at": (self._base + timedelta(seconds=sequence)).isoformat(),
            "correlation_id": self._correlation_id,
            "event_type": event_type,
            "payload": payload,
            "previous_hash": self.events[-1]["event_hash"]
            if self.events
            else "GENESIS",
        }
        if self._handoff_context:
            event_context = copy.deepcopy(self._handoff_context)
            event_context["artifact_type"] = "DOMAIN_EVENT"
            event_context["idempotency_key"] = (
                f"DOMAIN_EVENT:{self._correlation_id}:{sequence:04d}:{event_context['input_hash']}:{event_context['config_hash']}"
            )
            event["handoff_context"] = event_context
        event["event_hash"] = _sha256(event)
        self.events.append(event)


def verify_audit_chain(events: list[dict[str, Any]]) -> tuple[bool, int | None]:
    if not isinstance(events, list) or not events:
        return False, 1
    previous_hash = "GENESIS"
    for expected_sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return False, expected_sequence
        if (
            event.get("sequence") != expected_sequence
            or event.get("previous_hash") != previous_hash
        ):
            return False, expected_sequence
        candidate = {key: value for key, value in event.items() if key != "event_hash"}
        if _sha256(candidate) != event.get("event_hash"):
            return False, expected_sequence
        previous_hash = event["event_hash"]
    return True, None


class KittingPlanner:
    """确定性分析引擎。构造和运行都不访问网络或真实业务系统。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = copy.deepcopy(config)
        self._validate_config()
        self.config_hash = _sha256(self.config)
        self.external_write_count = 0

    @classmethod
    def from_config_file(cls, path: str | Path) -> "KittingPlanner":
        return cls(load_json(path))

    def _validate_config(self) -> None:
        _fields(
            self.config,
            {
                "schema_version",
                "config_version",
                "runtime",
                "planning",
                "quantity",
                "substitution",
                "supplier_scoring",
                "risk",
                "procurement",
                "iqc",
                "protected_invariants",
            },
            "config",
        )
        required = {
            "runtime": {"as_of_date", "generated_at", "external_side_effects_enabled"},
            "planning": {
                "material_need_offset_days",
                "allowed_warehouses",
                "eligible_inbound_statuses",
                "max_snapshot_age_hours",
                "calendar_mode",
            },
            "quantity": {"supported_uoms", "discrete_rounding"},
            "substitution": {"default_required_approval_roles"},
            "supplier_scoring": {
                "allowed_currencies",
                "weights",
                "late_day_penalty",
                "minimum_quality_pass_rate",
                "minimum_on_time_delivery_rate",
            },
            "risk": {"thresholds", "factor_points"},
            "procurement": {
                "draft_only",
                "major_purchase_threshold_cny",
                "major_material_criticalities",
                "major_required_approval_roles",
            },
            "iqc": {"default_inventory_state", "inspection_classes"},
        }
        for section, keys in required.items():
            _fields(self.config[section], keys, f"config.{section}")
        for section, key in (
            ("supplier_scoring", "weights"),
            ("risk", "thresholds"),
            ("risk", "factor_points"),
            ("iqc", "inspection_classes"),
        ):
            _fields(self.config[section][key], set(), f"config.{section}.{key}")
        _fields(self.config["protected_invariants"], set(), "protected_invariants")
        _fields(self.config.get("adapters", {}), set(), "adapters")
        as_of = _to_date(self.config["runtime"]["as_of_date"], "runtime.as_of_date")
        generated = _to_datetime(
            self.config["runtime"]["generated_at"], "runtime.generated_at"
        )
        if generated.date() != as_of:
            raise UnsafeConfigurationError(
                "as_of_date 必须与 generated_at 的本地日期一致"
            )
        if (
            self.config["quantity"]["supported_uoms"] != ["PCS"]
            or self.config["quantity"]["discrete_rounding"] != "CEILING"
        ):
            raise UnsafeConfigurationError(
                "当前版本支持 PCS 离散件，并在每条 BOM 边向上取整"
            )
        if self.config["supplier_scoring"]["allowed_currencies"] != ["CNY"]:
            raise UnsafeConfigurationError("当前版本只比较 CNY 报价，不进行币种换算")
        planning = self.config["planning"]
        if planning["calendar_mode"] != "CALENDAR_DAYS":
            raise UnsafeConfigurationError("当前版本使用自然日历")
        offset = planning["material_need_offset_days"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise UnsafeConfigurationError("material_need_offset_days 必须为非负整数")
        _positive(planning["max_snapshot_age_hours"], "max_snapshot_age_hours")
        if (
            not isinstance(planning["allowed_warehouses"], list)
            or not planning["allowed_warehouses"]
        ):
            raise UnsafeConfigurationError("allowed_warehouses 不得为空")
        _fields(
            self.config["risk"]["factor_points"],
            {
                "hard_shortage",
                "criticality_A",
                "criticality_B",
                "pending_substitution",
                "late_inbound",
                "no_eligible_quote",
                "low_supplier_otd",
                "selected_quote_late_per_day",
                "selected_quote_late_cap",
            },
            "risk.factor_points",
        )
        for key, value in self.config["risk"]["factor_points"].items():
            _whole_quantity(value, f"risk.factor_points.{key}")
        for policy in self.config["iqc"]["inspection_classes"].values():
            _fields(
                policy,
                {"sampling_plan", "required_documents", "inspection_items"},
                "iqc.inspection_classes",
            )
        if self.config.get("schema_version") != "1.0.0":
            raise UnsafeConfigurationError(
                "不支持的配置 schema_version",
                details={"schema_version": self.config.get("schema_version")},
            )
        if (
            self.config.get("runtime", {}).get("external_side_effects_enabled")
            is not False
        ):
            raise UnsafeConfigurationError("本地规划器 不允许启用外部副作用")

        invariants = self.config.get("protected_invariants", {})
        unsafe = sorted(
            name for name in PROTECTED_INVARIANTS if invariants.get(name) is not False
        )
        if unsafe:
            raise UnsafeConfigurationError(
                "受保护的不变量缺失或被打开",
                details={"unsafe_invariants": unsafe},
            )

        adapters = self.config.get("adapters", {})
        unsafe_adapters = {
            name: mode
            for name, mode in adapters.items()
            if mode not in {"disabled", "mock"}
        }
        if unsafe_adapters:
            raise UnsafeConfigurationError(
                "本地规划器 外部适配器只能是 disabled 或 mock",
                details={"unsafe_adapters": unsafe_adapters},
            )

        weights = self.config.get("supplier_scoring", {}).get("weights", {})
        required_weights = {"price", "delivery", "quality", "service"}
        if set(weights) != required_weights:
            raise UnsafeConfigurationError("供应商评分权重字段不完整")
        if any(
            _decimal(value, f"supplier_scoring.weights.{name}") < 0
            for name, value in weights.items()
        ):
            raise UnsafeConfigurationError("供应商评分权重不得为负")
        if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
            raise UnsafeConfigurationError("供应商评分权重之和必须为 1")

        thresholds = self.config.get("risk", {}).get("thresholds", {})
        amber = thresholds.get("amber")
        red = thresholds.get("red")
        if (
            not isinstance(amber, (int, float))
            or not isinstance(red, (int, float))
            or not (0 <= amber < red <= 100)
        ):
            raise UnsafeConfigurationError("风险阈值必须满足 0 <= amber < red <= 100")

        procurement = self.config.get("procurement", {})
        if procurement.get("draft_only") is not True:
            raise UnsafeConfigurationError("采购输出必须保持 draft_only=true")
        if (
            _non_negative(
                procurement.get("major_purchase_threshold_cny"),
                "major_purchase_threshold_cny",
            )
            < 0
        ):
            raise UnsafeConfigurationError("重大采购阈值不得为负")
        if not procurement.get("major_required_approval_roles"):
            raise UnsafeConfigurationError("重大采购审批角色不能为空")
        if not self.config.get("substitution", {}).get(
            "default_required_approval_roles"
        ):
            raise UnsafeConfigurationError("替代料审批角色不能为空")

    def execute_external_command(
        self, command: str, payload: dict[str, Any] | None = None
    ) -> None:
        """所有采购订单/自动审批/库存释放动作都在代码层恒定拒绝。"""
        if command in PROHIBITED_COMMANDS:
            raise ProhibitedAutomationError(
                f"命令 {command} 在 本地规划器 中被永久禁止",
                details={"command": command, "external_write_performed": False},
            )
        raise ValidationError(
            "未知外部命令",
            details={
                "command": command,
                "payload_keys": sorted((payload or {}).keys()),
            },
        )

    def run(self, scenario: dict[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(scenario)
        context = self._validate_and_index(data)
        input_hash = _sha256(data)
        plan_hash = _sha256({"input_hash": input_hash, "config_hash": self.config_hash})
        analysis_id = f"KA-{plan_hash[:16].upper()}"
        order_id = context["order"]["order_id"]
        batch_metadata = data.get("_batch_metadata", {})
        correlation_id = (
            batch_metadata.get("correlation_id")
            or f"CORR-{order_id}-{plan_hash[:8].upper()}"
        )
        order_lines = context["order"]["lines"]
        first_line = order_lines[0]
        common_uoms = {line.get("uom") for line in order_lines}
        handoff_base = {
            "schema_version": "1.0.0",
            "artifact_type": "PENDING",
            "case_id": batch_metadata.get("case_id") or correlation_id,
            "correlation_id": correlation_id,
            "inquiry_id": batch_metadata.get("inquiry_id")
            or context["order"].get("inquiry_id"),
            "quote_id": batch_metadata.get("quote_id"),
            "quote_version": batch_metadata.get("quote_version"),
            "order_id": order_id,
            "order_version": context["order"].get("order_version", 1),
            "customer_id": context["order"]["customer_id"],
            "product_id": first_line.get("product_id")
            if len(order_lines) == 1
            else None,
            "product_revision": first_line.get("product_revision")
            if len(order_lines) == 1
            else None,
            "bom_id": first_line.get("bom_id") if len(order_lines) == 1 else None,
            "bom_version": first_line.get("bom_version")
            if len(order_lines) == 1
            else None,
            "quantity": _number(
                sum(
                    (
                        _positive(line["ordered_qty"], "ordered_qty")
                        for line in order_lines
                    ),
                    Decimal("0"),
                )
            ),
            "uom": next(iter(common_uoms)) if len(common_uoms) == 1 else None,
            "currency": context["order"].get("currency", "CNY"),
            "tax_rate": context["order"].get("tax_rate"),
            "warehouse_id": context["order"]["warehouse_id"],
            "required_delivery_date": context["order"]["required_delivery_date"],
            "source_batch_ids": copy.deepcopy(
                batch_metadata.get("source_batch_ids", {})
            ),
            "source_snapshot_ids": copy.deepcopy(
                batch_metadata.get("source_snapshot_ids", [])
            ),
            "work_item_id": batch_metadata.get("work_item_id"),
            "trace_id": batch_metadata.get("trace_id"),
            "causation_event_id": batch_metadata.get("causation_event_id"),
            "raw_batch_hash": batch_metadata.get("raw_batch_hash"),
            "input_hash": input_hash,
            "config_version": self.config["config_version"],
            "config_hash": self.config_hash,
            "as_of_at": batch_metadata.get("as_of_at")
            or context["order"]["snapshot_at"],
            "source_mode": batch_metadata.get("source_mode", "LOCAL_JSON"),
            "idempotency_key": "PENDING",
        }

        def attach_handoff_context(
            artifact: dict[str, Any], artifact_type: str
        ) -> None:
            artifact_context = copy.deepcopy(handoff_base)
            artifact_context["artifact_type"] = artifact_type
            artifact_context["idempotency_key"] = (
                f"{artifact_type}:{order_id}:{artifact_context['order_version']}:{plan_hash}"
            )
            artifact["handoff_context"] = artifact_context

        audit = _AuditChain(
            self.config["runtime"]["generated_at"], correlation_id, handoff_base
        )

        audit.add(
            "ORDER_ACCEPTED",
            {
                "order_id": order_id,
                "input_hash": input_hash,
                "config_hash": self.config_hash,
                "external_write_performed": False,
            },
        )

        explosion = self._explode_bom(context, analysis_id, correlation_id)
        audit.add(
            "BOM_EXPLODED",
            {
                "analysis_id": analysis_id,
                "leaf_material_count": len(explosion["requirements"]),
            },
        )

        kitting = self._calculate_kitting(
            context, explosion, analysis_id, correlation_id
        )
        audit.add(
            "INVENTORY_NETTED",
            {
                "shortage_material_count_before_substitution": sum(
                    1
                    for line in kitting["lines"]
                    if line["pre_substitution_shortage"] > 0
                ),
                "frozen_qty_excluded": sum(
                    line["frozen_qty"] for line in kitting["lines"]
                ),
            },
        )
        audit.add(
            "SUBSTITUTIONS_EVALUATED",
            {
                "approved_proposals": sum(
                    1
                    for line in kitting["lines"]
                    for proposal in line["substitution_proposals"]
                    if proposal["status"] == "APPROVED_MANUAL_AND_APPLIED_TO_SIMULATION"
                ),
                "pending_proposals": sum(
                    1
                    for line in kitting["lines"]
                    for proposal in line["substitution_proposals"]
                    if proposal["status"] == "AWAITING_HUMAN_APPROVAL"
                ),
            },
        )

        comparisons = self._compare_supplier_quotes(
            context, kitting, analysis_id, correlation_id
        )
        audit.add(
            "SUPPLIER_QUOTES_COMPARED",
            {
                "material_count": len(comparisons["comparisons"]),
                "recommendations_are_advisory": True,
            },
        )

        alerts = self._assess_risks(
            context, kitting, comparisons, analysis_id, correlation_id
        )
        audit.add(
            "DELIVERY_RISKS_ASSESSED",
            {
                "red": sum(1 for alert in alerts["alerts"] if alert["level"] == "RED"),
                "amber": sum(
                    1 for alert in alerts["alerts"] if alert["level"] == "AMBER"
                ),
            },
        )

        draft = self._build_purchase_requisition(
            context, kitting, comparisons, alerts, analysis_id, correlation_id
        )
        audit.add(
            "PURCHASE_REQUISITION_DRAFTED",
            {
                "draft_id": draft["draft_id"],
                "line_count": len(draft["lines"]),
                "major_line_count": sum(
                    1 for line in draft["lines"] if line["major_purchase"]
                ),
                "purchase_order_created": False,
            },
        )

        iqc = self._build_iqc_handoff(context, draft, analysis_id, correlation_id)
        audit.add(
            "IQC_HANDOFF_DRAFTED",
            {
                "handoff_id": iqc["handoff_id"],
                "inspection_plan_count": len(iqc["inspection_plans"]),
                "inventory_released": False,
            },
        )

        prohibited_probe: dict[str, Any]
        try:
            self.execute_external_command(
                "create_purchase_order", {"draft_id": draft["draft_id"]}
            )
            prohibited_probe = {
                "passed": False,
                "unexpected": "command was not rejected",
            }  # pragma: no cover
        except ProhibitedAutomationError as exc:
            prohibited_probe = {
                "passed": True,
                "error_code": exc.code,
                "command": "create_purchase_order",
                "external_write_performed": False,
            }
            audit.add(
                "PROHIBITED_ACTION_REJECTED",
                {
                    "command": "create_purchase_order",
                    "error_code": exc.code,
                    "external_write_performed": False,
                },
            )

        chain_valid, invalid_sequence = verify_audit_chain(audit.events)
        summary = {
            "synthetic_example": data.get("_example", {}).get("synthetic") is True,
            "run_id": f"RUN-{plan_hash[:12].upper()}",
            "analysis_id": analysis_id,
            "correlation_id": correlation_id,
            "order_id": order_id,
            "config_version": self.config["config_version"],
            "config_hash": self.config_hash,
            "input_hash": input_hash,
            "generated_at": self.config["runtime"]["generated_at"],
            "leaf_material_count": len(explosion["requirements"]),
            "residual_shortage_material_count": sum(
                1 for line in kitting["lines"] if line["residual_shortage"] > 0
            ),
            "purchase_requisition_draft_lines": len(draft["lines"]),
            "major_purchase_lines_awaiting_human_approval": sum(
                1
                for line in draft["lines"]
                if line["major_purchase"]
                and line["approval_status"] == "AWAITING_HUMAN_APPROVAL"
            ),
            "external_write_count": self.external_write_count,
            "redline_checks": {
                "purchase_order_automation_rejected": prohibited_probe,
                "all_outputs_are_drafts_or_advisory": True,
                "substitution_requires_human_approval": True,
                "major_purchase_requires_human_approval": True,
                "iqc_release_requires_human_decision": True,
            },
            "audit_chain": {
                "valid": chain_valid,
                "first_invalid_sequence": invalid_sequence,
            },
            "status": "PLAN_COMPLETED_WITH_DRAFTS",
        }

        artifacts = {
            "bom_explosion": explosion,
            "kitting_analysis": kitting,
            "supplier_comparison": comparisons,
            "risk_alerts": alerts,
            "purchase_requisition_draft": draft,
            "iqc_handoff": iqc,
            "audit_log": {"events": audit.events, "chain_valid": chain_valid},
            "run_summary": summary,
        }
        for artifact_name, artifact in artifacts.items():
            attach_handoff_context(artifact, artifact_name.upper())

        return artifacts

    def _validate_and_index(self, data: dict[str, Any]) -> dict[str, Any]:
        _fields(data, set(SCENARIO_FILES), "scenario")
        for name in ("_example", "_batch_metadata"):
            if name in data:
                _fields(data[name], set(), name)
        row_fields = {
            "materials": {
                "material_id",
                "material_name",
                "material_type",
                "base_uom",
                "criticality",
                "inspection_class",
                "active",
            },
            "bom_items": {
                "bom_id",
                "bom_version",
                "parent_material_id",
                "component_material_id",
                "qty_per",
                "scrap_rate",
                "uom",
                "effective_from",
                "status",
            },
            "inventory_positions": {
                "material_id",
                "warehouse_id",
                "on_hand_qty",
                "frozen_qty",
                "quality_hold_qty",
                "allocated_qty",
                "safety_stock_qty",
                "snapshot_at",
            },
            "inbound_supply": {
                "inbound_line_id",
                "material_id",
                "warehouse_id",
                "ordered_qty",
                "received_qty",
                "cancelled_qty",
                "status",
                "snapshot_at",
            },
            "substitution_rules": {
                "rule_id",
                "source_material_id",
                "substitute_material_id",
                "conversion_factor",
                "max_share_percent",
                "applicable_product_ids",
                "effective_from",
                "active",
                "required_approval_roles",
            },
            "suppliers": {
                "supplier_id",
                "supplier_name",
                "qualification_status",
                "valid_until",
                "on_time_delivery_rate",
                "quality_pass_rate",
                "service_score",
            },
            "supplier_quotes": {
                "quote_id",
                "supplier_id",
                "material_id",
                "unit_price",
                "currency",
                "minimum_order_qty",
                "order_multiple",
                "capacity_qty",
                "lead_time_days",
                "valid_from",
                "valid_to",
                "tax_included",
                "freight_included",
                "source_ref",
            },
            "approval_decisions": {
                "approval_id",
                "scope",
                "subject_id",
                "decision",
                "actor_id",
                "actor_type",
                "role",
                "decided_at",
                "comment",
                "evidence_ref",
            },
        }
        for name, keys in row_fields.items():
            if not isinstance(data[name], list):
                raise ValidationError(f"{name} 必须是数组")
            for index, row in enumerate(data[name]):
                _fields(row, keys, f"{name}[{index}]")
        missing = sorted(set(SCENARIO_FILES) - set(data))
        if missing:
            raise ValidationError(
                "模拟数据集不完整", details={"missing_datasets": missing}
            )

        order = data["order"]
        if not isinstance(order, dict):
            raise ValidationError("order 必须是对象")
        required_order_fields = {
            "order_id",
            "customer_id",
            "order_date",
            "required_delivery_date",
            "warehouse_id",
            "status",
            "snapshot_at",
            "lines",
        }
        missing_order = sorted(
            field
            for field in required_order_fields
            if order.get(field) is None or order.get(field) == ""
        )
        if missing_order:
            raise ValidationError("订单必填字段缺失", details={"fields": missing_order})
        if order["status"] != "CONFIRMED":
            raise ValidationError(
                "只有 CONFIRMED 订单可进入 本地规划器 分析",
                details={"status": order["status"]},
            )
        if order.get("currency", "CNY") != "CNY":
            raise ValidationError("订单币种必须为 CNY")
        order_date = _to_date(order["order_date"], "order.order_date")
        delivery_date = _to_date(
            order["required_delivery_date"], "order.required_delivery_date"
        )
        if delivery_date < order_date:
            raise ValidationError("订单交期不得早于订单日期")
        if order["warehouse_id"] not in self.config["planning"]["allowed_warehouses"]:
            raise ValidationError(
                "订单仓库不在 本地规划器 允许范围",
                details={"warehouse_id": order["warehouse_id"]},
            )
        snapshot_at = _to_datetime(order["snapshot_at"], "order.snapshot_at")
        generated_at = _to_datetime(
            self.config["runtime"]["generated_at"], "runtime.generated_at"
        )
        if snapshot_at > generated_at:
            raise ValidationError("快照时点晚于计划时点")
        age_hours = abs((generated_at - snapshot_at).total_seconds()) / 3600
        if age_hours > float(self.config["planning"]["max_snapshot_age_hours"]):
            raise ValidationError(
                "订单/库存快照超过允许新鲜度",
                details={
                    "age_hours": age_hours,
                    "max_hours": self.config["planning"]["max_snapshot_age_hours"],
                },
            )
        if not isinstance(order["lines"], list) or not order["lines"]:
            raise ValidationError("订单至少需要一行")
        seen_order_lines: set[str] = set()
        for line in order["lines"]:
            _fields(
                line,
                {
                    "line_id",
                    "product_id",
                    "ordered_qty",
                    "uom",
                    "bom_id",
                    "bom_version",
                },
                "order.lines",
            )
            line_id = line.get("line_id")
            if not isinstance(line_id, str) or not line_id:
                raise ValidationError("订单 line_id 缺失")
            if line_id in seen_order_lines:
                raise ValidationError("订单 line_id 重复", details={"line_id": line_id})
            seen_order_lines.add(line_id)
            qty = _positive(
                line.get("ordered_qty"), f"order.lines[{line_id}].ordered_qty"
            )
            if qty != _floor(qty):
                raise ValidationError("PCS 订单数量必须为整数")
            if line.get("uom") not in self.config["quantity"]["supported_uoms"]:
                raise ValidationError(
                    "订单单位不受支持",
                    details={"line_id": line_id, "uom": line.get("uom")},
                )

        materials = _unique_index(data["materials"], "material_id", "materials")
        for material_id, material in materials.items():
            if not isinstance(material["active"], bool):
                raise ValidationError(
                    "物料 active 必须为布尔值", details={"material_id": material_id}
                )
            if material["material_type"] not in {
                "FINISHED",
                "SUBASSEMBLY",
                "PURCHASED",
            }:
                raise ValidationError(
                    "物料类型非法", details={"material_id": material_id}
                )
            if material.get("active") is not True:
                continue
            if (
                material.get("base_uom")
                not in self.config["quantity"]["supported_uoms"]
            ):
                raise ValidationError(
                    "物料基础单位不受支持", details={"material_id": material_id}
                )
            if material.get("criticality") not in {"A", "B", "C"}:
                raise ValidationError(
                    "物料关键等级非法", details={"material_id": material_id}
                )
            if (
                material.get("inspection_class")
                not in self.config["iqc"]["inspection_classes"]
            ):
                raise ValidationError(
                    "物料 IQC 等级非法", details={"material_id": material_id}
                )
        for line in order["lines"]:
            material = materials.get(line.get("product_id"))
            if not material or material.get("active") is not True:
                raise ValidationError(
                    "订单成品物料不存在或已停用",
                    details={"product_id": line.get("product_id")},
                )

        bom_items = data["bom_items"]
        if not isinstance(bom_items, list) or not bom_items:
            raise ValidationError("BOM 数据为空")
        seen_bom_edges: set[tuple[str, str, str, str]] = set()
        for index, row in enumerate(bom_items):
            parent = row.get("parent_material_id")
            component = row.get("component_material_id")
            if parent not in materials or component not in materials:
                raise ValidationError(
                    "BOM 父项或子项不存在",
                    details={"index": index, "parent": parent, "component": component},
                )
            if not materials[parent]["active"] or not materials[component]["active"]:
                raise ValidationError("BOM 引用了停用物料", details={"index": index})
            if row["uom"] != materials[component]["base_uom"]:
                raise ValidationError(
                    "BOM 单位与物料基础单位不一致", details={"index": index}
                )
            edge = (
                str(row.get("bom_id")),
                str(row.get("bom_version")),
                parent,
                component,
            )
            if edge in seen_bom_edges:
                raise ValidationError("BOM 边重复", details={"edge": edge})
            seen_bom_edges.add(edge)
            _positive(row.get("qty_per"), f"bom_items[{index}].qty_per")
            scrap = _non_negative(
                row.get("scrap_rate"), f"bom_items[{index}].scrap_rate"
            )
            if scrap >= 1:
                raise ValidationError("BOM 损耗率必须小于 1", details={"index": index})
            _to_date(row.get("effective_from"), f"bom_items[{index}].effective_from")
            if row.get("effective_to"):
                if _to_date(
                    row["effective_to"], f"bom_items[{index}].effective_to"
                ) < _to_date(
                    row["effective_from"], f"bom_items[{index}].effective_from"
                ):
                    raise ValidationError(
                        "BOM 失效日早于生效日", details={"index": index}
                    )

        inventory_positions = data["inventory_positions"]
        seen_inventory: set[tuple[str, str, str]] = set()
        for index, row in enumerate(inventory_positions):
            material_id = row.get("material_id")
            if material_id not in materials:
                raise ValidationError(
                    "库存物料不存在",
                    details={"index": index, "material_id": material_id},
                )
            key = (
                str(row.get("warehouse_id")),
                material_id,
                str(row.get("lot_scope", "ALL")),
            )
            if key in seen_inventory:
                raise ValidationError("库存快照键重复", details={"key": key})
            seen_inventory.add(key)
            row_snapshot = _to_datetime(
                row.get("snapshot_at"), f"inventory_positions[{index}].snapshot_at"
            )
            if abs((snapshot_at - row_snapshot).total_seconds()) > 1:
                raise ValidationError(
                    "库存快照时点与订单快照不一致", details={"index": index}
                )
            for field in (
                "on_hand_qty",
                "frozen_qty",
                "quality_hold_qty",
                "allocated_qty",
                "safety_stock_qty",
            ):
                _whole_quantity(row.get(field), f"inventory_positions[{index}].{field}")
            on_hand = _non_negative(
                row.get("on_hand_qty"), f"inventory_positions[{index}].on_hand_qty"
            )
            frozen = _non_negative(
                row.get("frozen_qty"), f"inventory_positions[{index}].frozen_qty"
            )
            quality_hold = _non_negative(
                row.get("quality_hold_qty"),
                f"inventory_positions[{index}].quality_hold_qty",
            )
            allocated = _non_negative(
                row.get("allocated_qty"), f"inventory_positions[{index}].allocated_qty"
            )
            safety_stock = _non_negative(
                row.get("safety_stock_qty"),
                f"inventory_positions[{index}].safety_stock_qty",
            )
            if frozen > on_hand:
                raise ValidationError(
                    "冻结量不得大于现存量",
                    details={
                        "index": index,
                        "on_hand_qty": _number(on_hand),
                        "frozen_qty": _number(frozen),
                    },
                )
            deductions = frozen + quality_hold + allocated + safety_stock
            if deductions > on_hand:
                raise ValidationError(
                    "冻结、待检、占用和安全库存合计不得大于现存量",
                    details={
                        "index": index,
                        "on_hand_qty": _number(on_hand),
                        "deductions": _number(deductions),
                    },
                )

        _unique_index(data["inbound_supply"], "inbound_line_id", "inbound_supply")
        for index, row in enumerate(data["inbound_supply"]):
            if row.get("material_id") not in materials:
                raise ValidationError("在途物料不存在", details={"index": index})
            if (
                abs(
                    (
                        _to_datetime(row["snapshot_at"], "inbound.snapshot_at")
                        - snapshot_at
                    ).total_seconds()
                )
                > 1
            ):
                raise ValidationError(
                    "在途快照时点与订单快照不一致", details={"index": index}
                )
            ordered = _whole_quantity(
                row.get("ordered_qty"), f"inbound_supply[{index}].ordered_qty"
            )
            received = _whole_quantity(
                row.get("received_qty"), f"inbound_supply[{index}].received_qty"
            )
            cancelled = _whole_quantity(
                row.get("cancelled_qty"), f"inbound_supply[{index}].cancelled_qty"
            )
            if received + cancelled > ordered:
                raise ValidationError(
                    "在途已收与取消量超过订单量", details={"index": index}
                )
            if row.get("expected_arrival_date"):
                _to_date(
                    row["expected_arrival_date"],
                    f"inbound_supply[{index}].expected_arrival_date",
                )

        suppliers = _unique_index(data["suppliers"], "supplier_id", "suppliers")
        for supplier in suppliers.values():
            _to_date(supplier["valid_until"], "supplier.valid_until")
            for field in (
                "on_time_delivery_rate",
                "quality_pass_rate",
                "service_score",
            ):
                if not 0 <= _decimal(supplier[field], field) <= 100:
                    raise ValidationError(
                        "供应商评分必须在 0–100 之间", details={"field": field}
                    )
        _unique_index(data["supplier_quotes"], "quote_id", "supplier_quotes")
        for index, quote in enumerate(data["supplier_quotes"]):
            if quote.get("supplier_id") not in suppliers:
                raise ValidationError("报价供应商不存在", details={"index": index})
            if quote.get("material_id") not in materials:
                raise ValidationError("报价物料不存在", details={"index": index})
            for field in (
                "unit_price",
                "minimum_order_qty",
                "order_multiple",
                "capacity_qty",
            ):
                _positive(quote.get(field), f"supplier_quotes[{index}].{field}")
                if field != "unit_price":
                    _whole_quantity(quote[field], f"supplier_quotes[{index}].{field}")
            lead = _non_negative(
                quote.get("lead_time_days"), f"supplier_quotes[{index}].lead_time_days"
            )
            if lead != _floor(lead):
                raise ValidationError("lead_time_days 必须为整数")
            if _to_date(quote["valid_from"], "quote.valid_from") > _to_date(
                quote["valid_to"], "quote.valid_to"
            ):
                raise ValidationError("报价失效日早于生效日")

        _unique_index(data["approval_decisions"], "approval_id", "approval_decisions")
        for index, decision in enumerate(data["approval_decisions"]):
            if decision.get("scope") not in {
                "SUBSTITUTION",
                "MAJOR_PURCHASE",
                "IQC_RELEASE",
            }:
                raise ValidationError(
                    "审批范围非法",
                    details={"index": index, "scope": decision.get("scope")},
                )
            if decision.get("decision") not in {"APPROVE", "REJECT"}:
                raise ValidationError(
                    "审批决定非法",
                    details={"index": index, "decision": decision.get("decision")},
                )
            if decision.get("actor_type") not in {
                "HUMAN",
                "SERVICE",
                "SYSTEM",
                "AGENT",
            }:
                raise ValidationError("审批 actor_type 非法", details={"index": index})
            for field in ("subject_id", "actor_id", "role", "comment", "evidence_ref"):
                if not isinstance(decision.get(field), str) or not decision[field]:
                    raise ValidationError(
                        "审批证据字段缺失", details={"index": index, "field": field}
                    )
            decided_at = _to_datetime(
                decision.get("decided_at"), f"approval_decisions[{index}].decided_at"
            )
            if decided_at > generated_at:
                raise ValidationError(
                    "不得使用晚于分析时点的审批决定", details={"index": index}
                )
            if (
                decision["scope"] == "SUBSTITUTION"
                and decision["decision"] == "APPROVE"
                and decision["actor_type"] == "HUMAN"
            ):
                limit = _positive(
                    decision.get("max_source_qty"), "approval.max_source_qty"
                )
                if limit != _floor(limit):
                    raise ValidationError("审批数量上限必须为整数")
        rule_index = _unique_index(
            data["substitution_rules"], "rule_id", "substitution_rules"
        )
        for rule_id, rule in rule_index.items():
            if not isinstance(rule["active"], bool):
                raise ValidationError(
                    "替代规则 active 必须为布尔值", details={"rule_id": rule_id}
                )
            if (
                rule.get("source_material_id") not in materials
                or rule.get("substitute_material_id") not in materials
            ):
                raise ValidationError(
                    "替代规则引用未知物料", details={"rule_id": rule_id}
                )
            if rule.get("source_material_id") == rule.get("substitute_material_id"):
                raise ValidationError(
                    "替代物料不能与原物料相同", details={"rule_id": rule_id}
                )
            if rule["active"] and any(
                not materials[rule[key]]["active"]
                for key in ("source_material_id", "substitute_material_id")
            ):
                raise ValidationError(
                    "有效替代规则引用了停用物料", details={"rule_id": rule_id}
                )
            _positive(
                rule.get("conversion_factor"),
                f"substitution_rules[{rule_id}].conversion_factor",
            )
            max_share = _positive(
                rule.get("max_share_percent"),
                f"substitution_rules[{rule_id}].max_share_percent",
            )
            if max_share > 100:
                raise ValidationError(
                    "替代比例不能超过 100%", details={"rule_id": rule_id}
                )
            if not rule.get("required_approval_roles"):
                raise ValidationError(
                    "替代规则审批角色不能为空", details={"rule_id": rule_id}
                )

        return {
            **data,
            "order": order,
            "materials_by_id": materials,
            "suppliers_by_id": suppliers,
            "as_of_date": _to_date(
                self.config["runtime"]["as_of_date"], "runtime.as_of_date"
            ),
            "generated_at": generated_at,
        }

    def _active_bom_rows(
        self,
        context: dict[str, Any],
        parent_material_id: str,
        requested_version: str | None = None,
        requested_bom_id: str | None = None,
    ) -> list[dict[str, Any]]:
        as_of = context["as_of_date"]
        rows = []
        for row in context["bom_items"]:
            if (
                row.get("parent_material_id") != parent_material_id
                or row.get("status") != "RELEASED"
            ):
                continue
            start = _to_date(row["effective_from"], "bom.effective_from")
            end = (
                _to_date(row["effective_to"], "bom.effective_to")
                if row.get("effective_to")
                else date.max
            )
            if start <= as_of <= end:
                rows.append(row)
        if requested_version:
            rows = [row for row in rows if row.get("bom_version") == requested_version]
        if requested_bom_id:
            rows = [row for row in rows if row.get("bom_id") == requested_bom_id]
        versions = {(row.get("bom_id"), row.get("bom_version")) for row in rows}
        if len(versions) > 1:
            raise ValidationError(
                "同一父项命中多个有效 BOM 版本",
                details={
                    "parent_material_id": parent_material_id,
                    "versions": sorted(versions),
                },
            )
        return sorted(
            rows, key=lambda row: (row["component_material_id"], row["bom_id"])
        )

    def _explode_bom(
        self,
        context: dict[str, Any],
        analysis_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        aggregate: dict[str, Decimal] = defaultdict(Decimal)
        sources: dict[str, list[dict[str, Any]]] = defaultdict(list)

        def visit(
            parent: str,
            parent_qty: Decimal,
            path: list[str],
            version_hint: str | None = None,
            bom_id_hint: str | None = None,
        ) -> None:
            if parent in path:
                cycle = path[path.index(parent) :] + [parent]
                raise BomCycleError("BOM 存在循环", details={"cycle_path": cycle})
            rows = self._active_bom_rows(context, parent, version_hint, bom_id_hint)
            if not rows:
                if context["materials_by_id"][parent]["material_type"] != "PURCHASED":
                    raise ValidationError(
                        "成品或子组件缺少有效 BOM", details={"material_id": parent}
                    )
                aggregate[parent] += parent_qty
                sources[parent].append(
                    {"path": path + [parent], "quantity": _number(parent_qty)}
                )
                return
            next_path = path + [parent]
            for row in rows:
                child = row["component_material_id"]
                raw = (
                    parent_qty
                    * _decimal(row["qty_per"], "qty_per")
                    * (Decimal("1") + _decimal(row["scrap_rate"], "scrap_rate"))
                )
                child_qty = _ceil(raw)
                visit(child, child_qty, next_path)

        for line in context["order"]["lines"]:
            product_id = line["product_id"]
            if not self._active_bom_rows(
                context, product_id, line.get("bom_version"), line.get("bom_id")
            ):
                raise ValidationError(
                    "订单成品没有唯一有效 BOM",
                    details={
                        "product_id": product_id,
                        "bom_version": line.get("bom_version"),
                    },
                )
            visit(
                product_id,
                _positive(line["ordered_qty"], "ordered_qty"),
                [],
                line.get("bom_version"),
                line.get("bom_id"),
            )

        requirements = [
            {
                "material_id": material_id,
                "material_name": context["materials_by_id"][material_id][
                    "material_name"
                ],
                "gross_requirement": _number(quantity),
                "uom": context["materials_by_id"][material_id]["base_uom"],
                "demand_sources": sources[material_id],
            }
            for material_id, quantity in sorted(aggregate.items())
        ]
        return {
            "analysis_id": analysis_id,
            "correlation_id": correlation_id,
            "order_id": context["order"]["order_id"],
            "as_of_date": context["as_of_date"].isoformat(),
            "calculation_rule": "ceil(parent_requirement * qty_per * (1 + scrap_rate)) at every BOM edge",
            "requirements": requirements,
            "external_write_performed": False,
        }

    def _availability(
        self,
        context: dict[str, Any],
        material_id: str,
        material_need_date: date,
    ) -> dict[str, Any]:
        allowed = set(self.config["planning"]["allowed_warehouses"])
        positions = [
            row
            for row in context["inventory_positions"]
            if row.get("material_id") == material_id
            and row.get("warehouse_id") in allowed
        ]
        fields = (
            "on_hand_qty",
            "frozen_qty",
            "quality_hold_qty",
            "allocated_qty",
            "safety_stock_qty",
        )
        totals = {
            field: sum(
                (_non_negative(row[field], field) for row in positions), Decimal("0")
            )
            for field in fields
        }
        deductions = (
            totals["frozen_qty"]
            + totals["quality_hold_qty"]
            + totals["allocated_qty"]
            + totals["safety_stock_qty"]
        )
        effective = max(totals["on_hand_qty"] - deductions, Decimal("0"))
        over_committed = deductions > totals["on_hand_qty"]

        timely = Decimal("0")
        late = Decimal("0")
        timely_lines: list[dict[str, Any]] = []
        late_lines: list[dict[str, Any]] = []
        excluded_lines: list[dict[str, Any]] = []
        eligible_statuses = set(self.config["planning"]["eligible_inbound_statuses"])
        for row in context["inbound_supply"]:
            if (
                row.get("material_id") != material_id
                or row.get("warehouse_id") not in allowed
            ):
                continue
            open_qty = max(
                _non_negative(row["ordered_qty"], "ordered_qty")
                - _non_negative(row["received_qty"], "received_qty")
                - _non_negative(row["cancelled_qty"], "cancelled_qty"),
                Decimal("0"),
            )
            if open_qty == 0:
                excluded_lines.append(
                    {"inbound_line_id": row["inbound_line_id"], "reason": "NO_OPEN_QTY"}
                )
                continue
            if row.get("status") not in eligible_statuses:
                excluded_lines.append(
                    {
                        "inbound_line_id": row["inbound_line_id"],
                        "open_qty": _number(open_qty),
                        "reason": "INELIGIBLE_STATUS",
                        "status": row.get("status"),
                    }
                )
                continue
            if not row.get("expected_arrival_date"):
                excluded_lines.append(
                    {
                        "inbound_line_id": row["inbound_line_id"],
                        "open_qty": _number(open_qty),
                        "reason": "MISSING_EXPECTED_ARRIVAL",
                    }
                )
                continue
            arrival = _to_date(row["expected_arrival_date"], "expected_arrival_date")
            detail = {
                "inbound_line_id": row["inbound_line_id"],
                "open_qty": _number(open_qty),
                "expected_arrival_date": arrival.isoformat(),
                "status": row["status"],
                "external_po_ref": row.get("external_po_ref"),
            }
            if arrival < context["as_of_date"]:
                late += open_qty
                detail["reason"] = "OVERDUE_PROMISE_REQUIRES_RECONFIRMATION"
                late_lines.append(detail)
                continue
            if arrival <= material_need_date:
                timely += open_qty
                timely_lines.append(detail)
            else:
                late += open_qty
                detail["days_late_to_need"] = (arrival - material_need_date).days
                late_lines.append(detail)

        return {
            **{field: _number(value) for field, value in totals.items()},
            "effective_on_hand": _number(effective),
            "over_committed_inventory": over_committed,
            "timely_inbound": _number(timely),
            "late_inbound": _number(late),
            "timely_inbound_lines": timely_lines,
            "late_inbound_lines": late_lines,
            "excluded_inbound_lines": excluded_lines,
            "total_available_by_need_date": _number(effective + timely),
        }

    def _calculate_kitting(
        self,
        context: dict[str, Any],
        explosion: dict[str, Any],
        analysis_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        need_date = _to_date(
            context["order"]["required_delivery_date"], "required_delivery_date"
        ) - timedelta(days=int(self.config["planning"]["material_need_offset_days"]))
        line_by_material: dict[str, dict[str, Any]] = {}
        gross_by_material = {
            item["material_id"]: _decimal(
                item["gross_requirement"], "gross_requirement"
            )
            for item in explosion["requirements"]
        }

        for requirement in explosion["requirements"]:
            material_id = requirement["material_id"]
            gross = _decimal(requirement["gross_requirement"], "gross_requirement")
            availability = self._availability(context, material_id, need_date)
            pre_shortage = max(
                gross
                - _decimal(availability["effective_on_hand"], "effective_on_hand")
                - _decimal(availability["timely_inbound"], "timely_inbound"),
                Decimal("0"),
            )
            material = context["materials_by_id"][material_id]
            line = {
                "material_id": material_id,
                "material_name": material["material_name"],
                "criticality": material["criticality"],
                "uom": material["base_uom"],
                "gross_requirement": _number(gross),
                **availability,
                "pre_substitution_shortage": _number(pre_shortage),
                "approved_substitution_offset": 0,
                "residual_shortage": _number(pre_shortage),
                "substitution_proposals": [],
                "demand_sources": requirement["demand_sources"],
            }
            line_by_material[material_id] = line

        as_of = context["as_of_date"]
        approvals_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for approval in context["approval_decisions"]:
            if approval.get("scope") == "SUBSTITUTION":
                approvals_by_subject[str(approval.get("subject_id"))].append(approval)

        substitute_pools: dict[str, Decimal] = {}
        used_approvals: dict[str, Decimal] = defaultdict(Decimal)
        demand_products = {
            item["material_id"]: {
                source["path"][0] for source in item["demand_sources"]
            }
            for item in explosion["requirements"]
        }

        def pool_for(material_id: str) -> Decimal:
            if material_id not in substitute_pools:
                availability = self._availability(context, material_id, need_date)
                own_requirement = gross_by_material.get(material_id, Decimal("0"))
                substitute_pools[material_id] = max(
                    _decimal(
                        availability["total_available_by_need_date"],
                        "total_available_by_need_date",
                    )
                    - own_requirement,
                    Decimal("0"),
                )
            return substitute_pools[material_id]

        rules = sorted(
            context["substitution_rules"],
            key=lambda row: (int(row.get("priority", 9999)), row["rule_id"]),
        )
        for material_id, line in sorted(line_by_material.items()):
            original_shortage = _decimal(
                line["pre_substitution_shortage"], "pre_substitution_shortage"
            )
            remaining = original_shortage
            if remaining <= 0:
                continue
            for rule in rules:
                if (
                    rule.get("source_material_id") != material_id
                    or rule.get("active") is not True
                ):
                    continue
                start = _to_date(rule["effective_from"], "substitution.effective_from")
                end = (
                    _to_date(rule["effective_to"], "substitution.effective_to")
                    if rule.get("effective_to")
                    else date.max
                )
                applicable = set(rule.get("applicable_product_ids", []))
                if not (start <= as_of <= end) or (
                    "*" not in applicable
                    and not demand_products[material_id].issubset(applicable)
                ):
                    continue
                substitute_id = rule["substitute_material_id"]
                proposal_id = (
                    f"SUB-{context['order']['order_id']}-{material_id}-{substitute_id}"
                )
                decisions = approvals_by_subject.get(proposal_id, [])
                valid_decisions: list[dict[str, Any]] = []
                invalid_decisions: list[dict[str, Any]] = []
                for decision in decisions:
                    missing_evidence = not decision.get("comment") or not decision.get(
                        "evidence_ref"
                    )
                    if decision.get("actor_type") != "HUMAN" or missing_evidence:
                        invalid_decisions.append(
                            {
                                "approval_id": decision.get("approval_id"),
                                "reason": "HUMAN_APPROVAL_REQUIRED"
                                if decision.get("actor_type") != "HUMAN"
                                else "EVIDENCE_REQUIRED",
                            }
                        )
                    else:
                        valid_decisions.append(decision)
                required_roles = set(rule["required_approval_roles"]) | set(
                    self.config["substitution"]["default_required_approval_roles"]
                )
                approved_roles = {
                    decision["role"]
                    for decision in valid_decisions
                    if decision.get("decision") == "APPROVE"
                }
                rejected_roles = {
                    decision["role"]
                    for decision in valid_decisions
                    if decision.get("decision") == "REJECT"
                }
                missing_roles = sorted(required_roles - approved_roles)
                is_approved = not rejected_roles and not missing_roles

                conversion = _positive(rule["conversion_factor"], "conversion_factor")
                available_substitute_qty = pool_for(substitute_id)
                equivalent_available = _floor(available_substitute_qty / conversion)
                max_share_qty = _floor(
                    original_shortage
                    * _decimal(rule["max_share_percent"], "max_share_percent")
                    / 100
                )
                candidate_offset = min(remaining, equivalent_available, max_share_qty)
                approved_limit = (
                    min(
                        (
                            _positive(d["max_source_qty"], "max_source_qty")
                            for d in valid_decisions
                            if d["decision"] == "APPROVE"
                            and d["role"] in required_roles
                        ),
                        default=Decimal("0"),
                    )
                    if is_approved
                    else Decimal("0")
                )
                available_approval = max(
                    approved_limit - used_approvals[proposal_id], Decimal("0")
                )
                applied_offset = (
                    min(candidate_offset, available_approval)
                    if is_approved
                    else Decimal("0")
                )
                substitute_qty_to_use = _ceil(applied_offset * conversion)
                if applied_offset > 0:
                    used_approvals[proposal_id] += applied_offset
                    substitute_pools[substitute_id] = max(
                        available_substitute_qty - substitute_qty_to_use, Decimal("0")
                    )
                    remaining -= applied_offset

                if rejected_roles:
                    status = "REJECTED_BY_HUMAN"
                elif is_approved:
                    status = "APPROVED_MANUAL_AND_APPLIED_TO_SIMULATION"
                else:
                    status = "AWAITING_HUMAN_APPROVAL"
                line["substitution_proposals"].append(
                    {
                        "proposal_id": proposal_id,
                        "rule_id": rule["rule_id"],
                        "source_material_id": material_id,
                        "substitute_material_id": substitute_id,
                        "conversion_factor": _number(conversion),
                        "max_share_percent": _number(
                            _decimal(rule["max_share_percent"], "max_share_percent")
                        ),
                        "substitute_available_qty_before_proposal": _number(
                            available_substitute_qty
                        ),
                        "proposed_source_offset_qty": _number(candidate_offset),
                        "proposed_substitute_usage_qty": _number(
                            _ceil(candidate_offset * conversion)
                        ),
                        "applied_source_offset_qty": _number(applied_offset),
                        "approved_source_qty_limit": _number(approved_limit),
                        "applied_substitute_usage_qty": _number(substitute_qty_to_use),
                        "required_approval_roles": sorted(required_roles),
                        "approved_roles": sorted(approved_roles),
                        "missing_approval_roles": missing_roles,
                        "rejected_roles": sorted(rejected_roles),
                        "valid_human_approval_ids": sorted(
                            d["approval_id"] for d in valid_decisions
                        ),
                        "invalid_decisions": invalid_decisions,
                        "status": status,
                        "master_bom_modified": False,
                    }
                )
                if remaining <= 0:
                    break
            line["approved_substitution_offset"] = _number(
                original_shortage - remaining
            )
            line["residual_shortage"] = _number(remaining)

        lines = []
        for material_id in sorted(line_by_material):
            line = line_by_material[material_id]
            line["kitting_status"] = (
                "COVERED" if line["residual_shortage"] == 0 else "SHORTAGE"
            )
            line["calculation"] = (
                "residual_shortage = max(gross_requirement - effective_on_hand - timely_inbound "
                "- approved_substitution_offset, 0)"
            )
            lines.append(line)

        return {
            "analysis_id": analysis_id,
            "correlation_id": correlation_id,
            "order_id": context["order"]["order_id"],
            "config_version": self.config["config_version"],
            "snapshot_at": context["order"]["snapshot_at"],
            "material_need_date": need_date.isoformat(),
            "lines": lines,
            "external_write_performed": False,
        }

    def _quote_rejection_reasons(
        self,
        quote: dict[str, Any],
        supplier: dict[str, Any],
        required_qty: Decimal,
        as_of: date,
    ) -> tuple[list[str], Decimal]:
        reasons: list[str] = []
        minimum = _positive(quote["minimum_order_qty"], "minimum_order_qty")
        multiple = _positive(quote["order_multiple"], "order_multiple")
        purchase_qty = max(required_qty, minimum)
        purchase_qty = _ceil(purchase_qty / multiple) * multiple
        if supplier.get("qualification_status") != "APPROVED":
            reasons.append("SUPPLIER_NOT_APPROVED")
        if _to_date(supplier["valid_until"], "supplier.valid_until") < as_of:
            reasons.append("SUPPLIER_QUALIFICATION_EXPIRED")
        if not (
            _to_date(quote["valid_from"], "quote.valid_from")
            <= as_of
            <= _to_date(quote["valid_to"], "quote.valid_to")
        ):
            reasons.append("QUOTE_EXPIRED_OR_NOT_EFFECTIVE")
        if (
            quote.get("currency")
            not in self.config["supplier_scoring"]["allowed_currencies"]
        ):
            reasons.append("UNSUPPORTED_CURRENCY")
        if (
            quote.get("tax_included") is not True
            or quote.get("freight_included") is not True
        ):
            reasons.append("PRICE_BASIS_NOT_COMPARABLE")
        if _decimal(quote["capacity_qty"], "capacity_qty") < purchase_qty:
            reasons.append("INSUFFICIENT_CAPACITY")
        if float(supplier.get("quality_pass_rate", 0)) < float(
            self.config["supplier_scoring"]["minimum_quality_pass_rate"]
        ):
            reasons.append("QUALITY_RATE_BELOW_MINIMUM")
        if float(supplier.get("on_time_delivery_rate", 0)) < float(
            self.config["supplier_scoring"]["minimum_on_time_delivery_rate"]
        ):
            reasons.append("OTD_RATE_BELOW_MINIMUM")
        return reasons, purchase_qty

    def _compare_supplier_quotes(
        self,
        context: dict[str, Any],
        kitting: dict[str, Any],
        analysis_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        as_of = context["as_of_date"]
        need_date = _to_date(kitting["material_need_date"], "material_need_date")
        weights = self.config["supplier_scoring"]["weights"]
        comparisons: list[dict[str, Any]] = []

        for line in kitting["lines"]:
            shortage = _decimal(line["residual_shortage"], "residual_shortage")
            if shortage <= 0:
                continue
            eligible: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for quote in sorted(
                context["supplier_quotes"], key=lambda row: row["quote_id"]
            ):
                if quote.get("material_id") != line["material_id"]:
                    continue
                supplier = context["suppliers_by_id"][quote["supplier_id"]]
                reasons, purchase_qty = self._quote_rejection_reasons(
                    quote, supplier, shortage, as_of
                )
                if reasons:
                    rejected.append(
                        {
                            "quote_id": quote["quote_id"],
                            "supplier_id": quote["supplier_id"],
                            "reasons": reasons,
                        }
                    )
                    continue
                arrival = as_of + timedelta(days=int(quote["lead_time_days"]))
                late_days = max((arrival - need_date).days, 0)
                eligible.append(
                    {
                        "quote_id": quote["quote_id"],
                        "supplier_id": quote["supplier_id"],
                        "supplier_name": supplier["supplier_name"],
                        "currency": quote["currency"],
                        "unit_price": float(quote["unit_price"]),
                        "suggested_purchase_qty": _number(purchase_qty),
                        "overbuy_qty": _number(purchase_qty - shortage),
                        "estimated_amount": _money(
                            purchase_qty * _decimal(quote["unit_price"], "unit_price")
                        ),
                        "lead_time_days": int(quote["lead_time_days"]),
                        "estimated_arrival_date": arrival.isoformat(),
                        "days_late_to_need": late_days,
                        "on_time_delivery_rate": float(
                            supplier["on_time_delivery_rate"]
                        ),
                        "quality_pass_rate": float(supplier["quality_pass_rate"]),
                        "service_score": float(supplier["service_score"]),
                        "source_ref": quote["source_ref"],
                    }
                )

            if eligible:
                lowest_price = min(item["unit_price"] for item in eligible)
                for item in eligible:
                    item["sub_scores"] = {
                        "price": round(lowest_price / item["unit_price"] * 100, 4),
                        "delivery": round(
                            max(
                                0,
                                100
                                - item["days_late_to_need"]
                                * float(
                                    self.config["supplier_scoring"]["late_day_penalty"]
                                ),
                            ),
                            4,
                        ),
                        "quality": round(item["quality_pass_rate"], 4),
                        "service": round(item["service_score"], 4),
                    }
                    item["weighted_score"] = round(
                        sum(
                            item["sub_scores"][name] * float(weights[name])
                            for name in weights
                        ),
                        4,
                    )
                eligible.sort(
                    key=lambda item: (
                        -item["weighted_score"],
                        item["estimated_amount"],
                        item["lead_time_days"],
                        item["quote_id"],
                    )
                )
                for rank, item in enumerate(eligible, start=1):
                    item["rank"] = rank
                selected = copy.deepcopy(eligible[0])
                selected["recommendation_type"] = "ADVISORY_RECOMMENDATION"
                selected["creates_supplier_commitment"] = False
            else:
                selected = None

            comparisons.append(
                {
                    "material_id": line["material_id"],
                    "residual_shortage": _number(shortage),
                    "uom": line["uom"],
                    "selected_quote": selected,
                    "eligible_quotes": eligible,
                    "rejected_quotes": rejected,
                    "weights": weights,
                    "supplier_awarded": False,
                    "external_message_sent": False,
                }
            )

        return {
            "analysis_id": analysis_id,
            "correlation_id": correlation_id,
            "as_of_date": as_of.isoformat(),
            "comparisons": comparisons,
            "recommendations_are_advisory": True,
            "external_write_performed": False,
        }

    def _assess_risks(
        self,
        context: dict[str, Any],
        kitting: dict[str, Any],
        comparisons: dict[str, Any],
        analysis_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        factors = self.config["risk"]["factor_points"]
        thresholds = self.config["risk"]["thresholds"]
        comparison_by_material = {
            item["material_id"]: item for item in comparisons["comparisons"]
        }
        alerts = []
        for line in kitting["lines"]:
            if line["residual_shortage"] <= 0:
                continue
            score = int(factors["hard_shortage"])
            reasons = ["HARD_SHORTAGE"]
            if line["criticality"] == "A":
                score += int(factors["criticality_A"])
                reasons.append("CRITICALITY_A")
            elif line["criticality"] == "B":
                score += int(factors["criticality_B"])
                reasons.append("CRITICALITY_B")
            if any(
                p["status"] == "AWAITING_HUMAN_APPROVAL"
                for p in line["substitution_proposals"]
            ):
                score += int(factors["pending_substitution"])
                reasons.append("SUBSTITUTION_APPROVAL_PENDING")
            if line["late_inbound"] > 0:
                score += int(factors["late_inbound"])
                reasons.append("LATE_INBOUND_NOT_COUNTED")

            comparison = comparison_by_material[line["material_id"]]
            selected = comparison["selected_quote"]
            if not selected:
                score += int(factors["no_eligible_quote"])
                reasons.append("NO_ELIGIBLE_QUOTE")
            else:
                late_points = min(
                    selected["days_late_to_need"]
                    * int(factors["selected_quote_late_per_day"]),
                    int(factors["selected_quote_late_cap"]),
                )
                if late_points:
                    score += late_points
                    reasons.append("SELECTED_QUOTE_LATE")
                if selected["on_time_delivery_rate"] < float(
                    self.config["supplier_scoring"]["minimum_on_time_delivery_rate"]
                ):
                    score += int(factors["low_supplier_otd"])
                    reasons.append("LOW_SUPPLIER_OTD")

            score = min(score, 100)
            level = (
                "RED"
                if score >= thresholds["red"]
                else "AMBER"
                if score >= thresholds["amber"]
                else "GREEN"
            )
            owner_roles = ["PROCUREMENT_BUYER"]
            if "SUBSTITUTION_APPROVAL_PENDING" in reasons:
                owner_roles.extend(["ENGINEERING_APPROVER", "QUALITY_APPROVER"])
            recommended_actions = [
                "人工复核缺料数量与库存快照",
                "人工确认供应商交期并决定是否提交采购申请草稿",
            ]
            if "SUBSTITUTION_APPROVAL_PENDING" in reasons:
                recommended_actions.append("补齐替代料自然人审批，审批前不得冲减缺料")
            if "NO_ELIGIBLE_QUOTE" in reasons:
                recommended_actions.append("人工发起询价并录入可比报价")
            alerts.append(
                {
                    "alert_id": f"RISK-{context['order']['order_id']}-{line['material_id']}",
                    "material_id": line["material_id"],
                    "level": level,
                    "score": score,
                    "reason_codes": reasons,
                    "residual_shortage": line["residual_shortage"],
                    "material_need_date": kitting["material_need_date"],
                    "selected_estimated_arrival_date": selected[
                        "estimated_arrival_date"
                    ]
                    if selected
                    else None,
                    "owner_roles": owner_roles,
                    "recommended_actions": recommended_actions,
                    "decision_is_human_owned": True,
                }
            )
        return {
            "analysis_id": analysis_id,
            "correlation_id": correlation_id,
            "alerts": sorted(
                alerts, key=lambda item: (-item["score"], item["material_id"])
            ),
            "thresholds": thresholds,
            "external_write_performed": False,
        }

    def _build_purchase_requisition(
        self,
        context: dict[str, Any],
        kitting: dict[str, Any],
        comparisons: dict[str, Any],
        alerts: dict[str, Any],
        analysis_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        comparison_by_material = {
            item["material_id"]: item for item in comparisons["comparisons"]
        }
        alert_by_material = {item["material_id"]: item for item in alerts["alerts"]}
        lines = []
        threshold = Decimal(
            str(self.config["procurement"]["major_purchase_threshold_cny"])
        )
        criticalities = set(self.config["procurement"]["major_material_criticalities"])
        for kitting_line in kitting["lines"]:
            shortage = _decimal(kitting_line["residual_shortage"], "residual_shortage")
            if shortage <= 0:
                continue
            comparison = comparison_by_material[kitting_line["material_id"]]
            selected = comparison["selected_quote"]
            suggested_qty = (
                _decimal(selected["suggested_purchase_qty"], "suggested_purchase_qty")
                if selected
                else _ceil(shortage)
            )
            unit_price = Decimal(str(selected["unit_price"])) if selected else None
            amount = suggested_qty * unit_price if unit_price is not None else None
            is_major = kitting_line["criticality"] in criticalities or (
                amount is not None and amount >= threshold
            )
            lines.append(
                {
                    "line_id": f"PRL-{len(lines) + 1:03d}",
                    "material_id": kitting_line["material_id"],
                    "material_name": kitting_line["material_name"],
                    "uom": kitting_line["uom"],
                    "source_shortage_qty": _number(shortage),
                    "suggested_purchase_qty": _number(suggested_qty),
                    "overbuy_qty": _number(suggested_qty - shortage),
                    "target_arrival_date": kitting["material_need_date"],
                    "recommended_supplier_id": selected["supplier_id"]
                    if selected
                    else None,
                    "recommended_quote_id": selected["quote_id"] if selected else None,
                    "recommendation_is_advisory": True,
                    "estimated_unit_price_cny": float(unit_price)
                    if unit_price is not None
                    else None,
                    "estimated_line_amount_cny": _money(amount)
                    if amount is not None
                    else None,
                    "risk_level": alert_by_material[kitting_line["material_id"]][
                        "level"
                    ],
                    "major_purchase": is_major,
                    "major_purchase_reasons": [
                        reason
                        for reason, applies in (
                            (
                                "CRITICAL_MATERIAL",
                                kitting_line["criticality"] in criticalities,
                            ),
                            (
                                "AMOUNT_THRESHOLD",
                                amount is not None and amount >= threshold,
                            ),
                        )
                        if applies
                    ],
                    "required_approval_roles": self.config["procurement"][
                        "major_required_approval_roles"
                    ]
                    if is_major
                    else [],
                    "approval_status": "AWAITING_HUMAN_APPROVAL"
                    if is_major
                    else "NOT_REQUIRED_FOR_DRAFT",
                    "external_purchase_order_ref": None,
                }
            )
        draft_id = f"PRD-{analysis_id.removeprefix('KA-')}"
        return {
            "draft_id": draft_id,
            "document_type": "PURCHASE_REQUISITION_DRAFT",
            "analysis_id": analysis_id,
            "correlation_id": correlation_id,
            "order_id": context["order"]["order_id"],
            "requester_id": context["order"].get("requester_id"),
            "draft_only": True,
            "status": "DRAFT_AWAITING_HUMAN_REVIEW",
            "currency": "CNY",
            "lines": lines,
            "controls": {
                "purchase_order_created": False,
                "purchase_order_released": False,
                "purchase_order_sent": False,
                "automatic_supplier_award": False,
                "major_purchase_requires_human_approval": True,
                "requester_cannot_approve_own_major_purchase": True,
            },
            "external_write_performed": False,
        }

    def _build_iqc_handoff(
        self,
        context: dict[str, Any],
        draft: dict[str, Any],
        analysis_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        inspection_plans = []
        for line in draft["lines"]:
            material = context["materials_by_id"][line["material_id"]]
            inspection_class = material["inspection_class"]
            policy = self.config["iqc"]["inspection_classes"][inspection_class]
            inspection_plans.append(
                {
                    "source_pr_line_id": line["line_id"],
                    "material_id": line["material_id"],
                    "inspection_class": inspection_class,
                    "sampling_plan": policy["sampling_plan"],
                    "required_documents": policy["required_documents"],
                    "inspection_items": policy["inspection_items"],
                    "expected_qty": line["suggested_purchase_qty"],
                    "external_po_ref": None,
                    "receipt_id": None,
                    "inventory_state_on_receipt": self.config["iqc"][
                        "default_inventory_state"
                    ],
                    "release_requires_human_iqc": True,
                    "automatic_inventory_release": False,
                }
            )
        return {
            "handoff_id": f"IQC-{analysis_id.removeprefix('KA-')}",
            "analysis_id": analysis_id,
            "correlation_id": correlation_id,
            "purchase_requisition_draft_id": draft["draft_id"],
            "status": "WAITING_FOR_MANUAL_PURCHASE_ORDER_AND_RECEIPT",
            "default_inventory_state": self.config["iqc"]["default_inventory_state"],
            "inspection_plans": inspection_plans,
            "activation_requirements": [
                "人工在既有系统创建的外部采购订单引用",
                "人工/既有系统收货回执",
                "供应商批次与实收数量",
                "所需质量文件引用",
            ],
            "external_write_performed": False,
        }
