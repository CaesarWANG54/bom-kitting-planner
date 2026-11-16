from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from bom_kitting_planner import (
    BomCycleError,
    KittingPlanner,
    ProhibitedAutomationError,
    UnsafeConfigurationError,
    ValidationError,
    load_json,
    load_scenario,
    verify_audit_chain,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "assembly"


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.config = load_json(EXAMPLE / "config.json")
        self.scenario = load_scenario(EXAMPLE / "scenario.json")

    def run_plan(self):
        return KittingPlanner(self.config).run(self.scenario)

    def material_lines(self):
        return {
            row["material_id"]: row
            for row in self.run_plan()["kitting_analysis"]["lines"]
        }

    def test_multilevel_bom_matches_hand_calculation(self):
        result = self.run_plan()["bom_explosion"]
        values = {
            row["material_id"]: row["gross_requirement"]
            for row in result["requirements"]
        }
        # 50 units -> ceil(50 * 1.02) = 51 assemblies -> ceil(51 * 1.04) = 54 housings.
        self.assertEqual(
            values,
            {
                "COMPONENT-HOUSING": 54,
                "COMPONENT-FASTENER": 204,
                "COMPONENT-CONTROL": 51,
                "COMPONENT-POWER": 50,
                "COMPONENT-GASKET": 50,
                "COMPONENT-CABLE": 100,
            },
        )
        housing = next(
            row
            for row in result["requirements"]
            if row["material_id"] == "COMPONENT-HOUSING"
        )
        self.assertEqual(
            housing["demand_sources"][0]["path"],
            ["PRODUCT-A", "ASSEMBLY-A", "COMPONENT-HOUSING"],
        )

    def test_stock_deductions_and_open_inbound_quantities(self):
        lines = self.material_lines()
        self.assertEqual(lines["COMPONENT-HOUSING"]["effective_on_hand"], 50)
        self.assertEqual(lines["COMPONENT-FASTENER"]["effective_on_hand"], 150)
        self.assertEqual(lines["COMPONENT-FASTENER"]["timely_inbound"], 60)
        self.assertEqual(lines["COMPONENT-POWER"]["late_inbound"], 20)
        self.assertEqual(lines["COMPONENT-POWER"]["timely_inbound"], 0)
        self.assertEqual(lines["COMPONENT-POWER"]["residual_shortage"], 40)

    def test_only_complete_human_approvals_offset_shortages(self):
        lines = self.material_lines()
        self.assertEqual(lines["COMPONENT-CONTROL"]["approved_substitution_offset"], 36)
        self.assertEqual(lines["COMPONENT-CONTROL"]["residual_shortage"], 0)
        proposal = lines["COMPONENT-POWER"]["substitution_proposals"][0]
        self.assertEqual(proposal["proposed_source_offset_qty"], 20)
        self.assertEqual(proposal["applied_source_offset_qty"], 0)
        self.assertEqual(proposal["missing_approval_roles"], ["QUALITY_APPROVER"])
        self.assertEqual(
            proposal["invalid_decisions"][0]["reason"], "HUMAN_APPROVAL_REQUIRED"
        )

    def test_substitution_respects_approved_quantity(self):
        self.scenario["approval_decisions"][0]["max_source_qty"] = 10
        line = self.material_lines()["COMPONENT-CONTROL"]
        self.assertEqual(line["approved_substitution_offset"], 10)
        self.assertEqual(line["residual_shortage"], 26)

    def test_inactive_or_malformed_substitute_material_is_rejected(self):
        alternative = next(
            row
            for row in self.scenario["materials"]
            if row["material_id"] == "ALTERNATIVE-CONTROL"
        )
        for value in (False, "true"):
            alternative["active"] = value
            with self.subTest(active=value), self.assertRaises(ValidationError):
                self.run_plan()

    def test_approval_limit_cannot_be_reused_by_another_rule(self):
        self.scenario["substitution_rules"][0]["max_share_percent"] = 10
        duplicate = copy.deepcopy(self.scenario["substitution_rules"][0])
        duplicate["rule_id"] = "RULE-CONTROL-SECOND"
        self.scenario["substitution_rules"].append(duplicate)
        for decision in self.scenario["approval_decisions"][:2]:
            decision["max_source_qty"] = 4
        self.assertEqual(
            self.material_lines()["COMPONENT-CONTROL"]["approved_substitution_offset"],
            4,
        )

    def test_rule_cannot_remove_policy_required_role(self):
        self.scenario["substitution_rules"][0]["required_approval_roles"] = [
            "ENGINEERING_APPROVER"
        ]
        self.scenario["approval_decisions"][1]["actor_type"] = "SERVICE"
        self.assertEqual(
            self.material_lines()["COMPONENT-CONTROL"]["approved_substitution_offset"],
            0,
        )

    def test_human_rejection_and_future_decision_are_not_accepted(self):
        self.scenario["approval_decisions"][1]["decision"] = "REJECT"
        self.assertEqual(
            self.material_lines()["COMPONENT-CONTROL"]["approved_substitution_offset"],
            0,
        )
        self.scenario["approval_decisions"][1]["decided_at"] = (
            "2026-11-17T10:00:00+08:00"
        )
        with self.assertRaises(ValidationError):
            self.run_plan()

    def test_shared_substitute_inventory_is_not_counted_twice(self):
        self.scenario["substitution_rules"][1]["substitute_material_id"] = (
            "ALTERNATIVE-CONTROL"
        )
        for decision in self.scenario["approval_decisions"][2:]:
            decision["subject_id"] = "SUB-ORDER-001-COMPONENT-POWER-ALTERNATIVE-CONTROL"
            decision["actor_type"] = "HUMAN"
        lines = self.material_lines()
        self.assertEqual(lines["COMPONENT-CONTROL"]["approved_substitution_offset"], 36)
        self.assertEqual(lines["COMPONENT-POWER"]["approved_substitution_offset"], 0)

    def test_substitute_keeps_supply_for_its_own_bom_demand(self):
        edge = copy.deepcopy(self.scenario["bom_items"][-1])
        edge.update(component_material_id="ALTERNATIVE-CONTROL", qty_per=1)
        self.scenario["bom_items"].append(edge)
        lines = self.material_lines()
        self.assertEqual(lines["ALTERNATIVE-CONTROL"]["gross_requirement"], 50)
        self.assertEqual(lines["ALTERNATIVE-CONTROL"]["residual_shortage"], 14)
        self.assertEqual(lines["COMPONENT-CONTROL"]["approved_substitution_offset"], 0)

    def test_product_specific_rule_cannot_cover_another_products_demand(self):
        product = copy.deepcopy(self.scenario["materials"][0])
        product["material_id"] = "PRODUCT-B"
        self.scenario["materials"].append(product)
        edge = copy.deepcopy(
            next(
                row
                for row in self.scenario["bom_items"]
                if row["component_material_id"] == "COMPONENT-CONTROL"
            )
        )
        edge.update(parent_material_id="PRODUCT-B", bom_id="BOM-PRODUCT-B")
        self.scenario["bom_items"].append(edge)
        line = copy.deepcopy(self.scenario["order"]["lines"][0])
        line.update(
            line_id="LINE-002",
            product_id="PRODUCT-B",
            ordered_qty=10,
            bom_id="BOM-PRODUCT-B",
        )
        self.scenario["order"]["lines"].append(line)
        self.assertEqual(
            self.material_lines()["COMPONENT-CONTROL"]["approved_substitution_offset"],
            0,
        )

    def test_supplier_ranking_keeps_rejections_and_pack_rounding(self):
        comparisons = {
            row["material_id"]: row
            for row in self.run_plan()["supplier_comparison"]["comparisons"]
        }
        power = comparisons["COMPONENT-POWER"]
        self.assertEqual(power["selected_quote"]["quote_id"], "QUOTE-POWER-A")
        self.assertEqual(power["selected_quote"]["suggested_purchase_qty"], 40)
        self.assertEqual(
            power["selected_quote"]["recommendation_type"], "ADVISORY_RECOMMENDATION"
        )
        self.assertIn(
            "SUPPLIER_QUALIFICATION_EXPIRED", power["rejected_quotes"][0]["reasons"]
        )
        gasket = comparisons["COMPONENT-GASKET"]
        self.assertEqual(gasket["selected_quote"]["suggested_purchase_qty"], 40)
        self.assertEqual(gasket["selected_quote"]["overbuy_qty"], 10)
        self.assertIn(
            "PRICE_BASIS_NOT_COMPARABLE", gasket["rejected_quotes"][0]["reasons"]
        )
        self.assertFalse(power["supplier_awarded"])

    def test_purchase_outputs_are_drafts_and_critical_items_need_review(self):
        outputs = self.run_plan()
        draft = outputs["purchase_requisition_draft"]
        lines = {row["material_id"]: row for row in draft["lines"]}
        self.assertTrue(draft["draft_only"])
        self.assertFalse(draft["controls"]["purchase_order_created"])
        self.assertEqual(
            lines["COMPONENT-POWER"]["approval_status"], "AWAITING_HUMAN_APPROVAL"
        )
        self.assertEqual(lines["COMPONENT-POWER"]["estimated_line_amount_cny"], 1000)
        self.assertEqual(lines["COMPONENT-GASKET"]["estimated_line_amount_cny"], 100)
        self.assertFalse(lines["COMPONENT-GASKET"]["major_purchase"])
        self.assertEqual(outputs["run_summary"]["external_write_count"], 0)

    def test_unquoted_shortage_stays_unknown_instead_of_zero_price(self):
        self.scenario["supplier_quotes"] = []
        outputs = self.run_plan()
        for row in outputs["purchase_requisition_draft"]["lines"]:
            self.assertIsNone(row["estimated_line_amount_cny"])
            self.assertIsNone(row["recommended_supplier_id"])
        self.assertTrue(
            all(
                "NO_ELIGIBLE_QUOTE" in row["reason_codes"]
                for row in outputs["risk_alerts"]["alerts"]
            )
        )

    def test_money_rounds_half_up_to_cents(self):
        self.scenario["supplier_quotes"][3]["unit_price"] = "2.500125"
        lines = {
            row["material_id"]: row
            for row in self.run_plan()["purchase_requisition_draft"]["lines"]
        }
        self.assertEqual(lines["COMPONENT-GASKET"]["estimated_line_amount_cny"], 100.01)

    def test_iqc_handoff_requires_manual_receipt_and_release(self):
        result = self.run_plan()["iqc_handoff"]
        self.assertEqual(result["default_inventory_state"], "QUARANTINE")
        self.assertTrue(
            all(
                row["release_requires_human_iqc"]
                and not row["automatic_inventory_release"]
                for row in result["inspection_plans"]
            )
        )

    def test_purchase_order_commands_and_unsafe_configuration_are_rejected(self):
        planner = KittingPlanner(self.config)
        for command in (
            "create_purchase_order",
            "release_purchase_order",
            "send_purchase_order",
            "auto_approve_substitution",
            "auto_approve_major_purchase",
            "release_iqc_inventory",
        ):
            with (
                self.subTest(command=command),
                self.assertRaises(ProhibitedAutomationError),
            ):
                planner.execute_external_command(command)
        for invariant in self.config["protected_invariants"]:
            config = copy.deepcopy(self.config)
            config["protected_invariants"][invariant] = True
            with (
                self.subTest(invariant=invariant),
                self.assertRaises(UnsafeConfigurationError),
            ):
                KittingPlanner(config)

    def test_bom_cycle_reports_the_cycle(self):
        edge = copy.deepcopy(self.scenario["bom_items"][0])
        edge.update(
            parent_material_id="COMPONENT-HOUSING",
            component_material_id="PRODUCT-A",
            bom_id="BOM-CYCLE",
        )
        self.scenario["bom_items"].append(edge)
        with self.assertRaises(BomCycleError) as error:
            self.run_plan()
        self.assertIn("PRODUCT-A", error.exception.details["cycle_path"])

    def test_missing_subassembly_bom_cannot_become_a_purchased_leaf(self):
        self.scenario["bom_items"] = [
            row
            for row in self.scenario["bom_items"]
            if row["parent_material_id"] != "ASSEMBLY-A"
        ]
        with self.assertRaises(ValidationError):
            self.run_plan()

    def test_selected_bom_id_is_honored(self):
        self.scenario["order"]["lines"][0]["bom_id"] = "BOM-MISSING"
        with self.assertRaises(ValidationError):
            self.run_plan()

    def test_wrong_bom_unit_and_inactive_material_are_rejected(self):
        self.scenario["bom_items"][0]["uom"] = "BOX"
        with self.assertRaises(ValidationError):
            self.run_plan()
        self.scenario["bom_items"][0]["uom"] = "PCS"
        self.scenario["materials"][2]["active"] = False
        with self.assertRaises(ValidationError):
            self.run_plan()

    def test_stale_and_inconsistent_snapshots_are_rejected(self):
        self.scenario["order"]["snapshot_at"] = "2026-11-14T09:00:00+08:00"
        with self.assertRaises(ValidationError):
            self.run_plan()
        self.scenario = load_scenario(EXAMPLE / "scenario.json")
        self.scenario["inbound_supply"][0]["snapshot_at"] = "2026-11-15T09:00:00+08:00"
        with self.assertRaises(ValidationError):
            self.run_plan()

    def test_overdue_arrival_promise_is_not_counted_as_ready_supply(self):
        self.scenario["inbound_supply"][1]["expected_arrival_date"] = "2026-11-15"
        line = self.material_lines()["COMPONENT-FASTENER"]
        self.assertEqual(line["timely_inbound"], 0)
        self.assertEqual(line["residual_shortage"], 54)
        self.assertEqual(
            line["late_inbound_lines"][0]["reason"],
            "OVERDUE_PROMISE_REQUIRES_RECONFIRMATION",
        )

    def test_missing_or_unconfirmed_inbound_date_cannot_cover_shortage(self):
        self.scenario["inbound_supply"][1]["expected_arrival_date"] = None
        self.assertEqual(
            self.material_lines()["COMPONENT-FASTENER"]["timely_inbound"], 0
        )
        self.scenario["inbound_supply"][1].update(
            expected_arrival_date="2026-11-20", status="UNCONFIRMED"
        )
        self.assertEqual(
            self.material_lines()["COMPONENT-FASTENER"]["timely_inbound"], 0
        )

    def test_invalid_quantities_and_dataset_shapes_are_rejected(self):
        original = copy.deepcopy(self.scenario)
        for value in (-1, True, "NaN", "Infinity", 70.5):
            self.scenario = copy.deepcopy(original)
            self.scenario["inventory_positions"][0]["on_hand_qty"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.run_plan()
        self.scenario = copy.deepcopy(original)
        self.scenario["materials"] = {}
        with self.assertRaises(ValidationError):
            self.run_plan()

    def test_future_snapshot_is_rejected(self):
        self.scenario["order"]["snapshot_at"] = "2026-11-16T11:00:00+08:00"
        with self.assertRaises(ValidationError):
            self.run_plan()

    def test_unsupported_units_and_currencies_cannot_be_enabled(self):
        self.config["quantity"]["supported_uoms"] = ["KG"]
        with self.assertRaises(UnsafeConfigurationError):
            KittingPlanner(self.config)
        self.config = load_json(EXAMPLE / "config.json")
        self.config["supplier_scoring"]["allowed_currencies"] = ["CNY", "USD"]
        with self.assertRaises(UnsafeConfigurationError):
            KittingPlanner(self.config)

    def test_determinism_input_immutability_and_no_network(self):
        before = copy.deepcopy(self.scenario)
        with patch(
            "socket.create_connection", side_effect=AssertionError("network forbidden")
        ):
            first, second = self.run_plan(), self.run_plan()
        self.assertEqual(first, second)
        self.assertEqual(before, self.scenario)

    def test_config_changes_produce_distinct_plan_and_idempotency_keys(self):
        first = self.run_plan()
        self.config["procurement"]["major_purchase_threshold_cny"] = 50
        second = self.run_plan()
        self.assertNotEqual(
            first["run_summary"]["analysis_id"], second["run_summary"]["analysis_id"]
        )
        self.assertNotEqual(
            first["purchase_requisition_draft"]["handoff_context"]["idempotency_key"],
            second["purchase_requisition_draft"]["handoff_context"]["idempotency_key"],
        )

    def test_audit_chain_detects_edits_and_reordering(self):
        events = self.run_plan()["audit_log"]["events"]
        self.assertEqual(verify_audit_chain(events), (True, None))
        modified = copy.deepcopy(events)
        modified[2]["payload"]["frozen_qty_excluded"] += 1
        self.assertEqual(verify_audit_chain(modified), (False, 3))
        self.assertFalse(verify_audit_chain(list(reversed(events)))[0])
        self.assertFalse(verify_audit_chain([])[0])
        self.assertFalse(verify_audit_chain(["invalid"])[0])


if __name__ == "__main__":
    unittest.main()
