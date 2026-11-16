from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bom_kitting_planner import (
    KittingPlanner,
    ValidationError,
    load_json,
    load_scenario,
)
from bom_kitting_planner.reporting import write_outputs

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "assembly"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bom test ")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.input = EXAMPLE / "scenario.json"
        self.config = EXAMPLE / "config.json"

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "bom_kitting_planner", *map(str, args)],
            cwd=self.folder,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
        )

    def test_installed_cli_works_outside_checkout_and_exports_auditable_plan(self):
        output = self.folder / "plan results"
        result = self.cli(
            "plan", "--input", self.input, "--config", self.config, "--output", output
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["shortage_materials"], 2)
        self.assertEqual(len(list(output.iterdir())), 11)
        with (output / "materials.csv").open(encoding="utf-8", newline="") as handle:
            rows = {row["material_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(rows["COMPONENT-POWER"]["residual_shortage"], "40")
        self.assertIn("合成示例", (output / "summary.md").read_text(encoding="utf-8"))
        audit = self.cli("audit", output / "audit_log.json")
        self.assertEqual(audit.returncode, 0, audit.stderr)
        data = load_json(output / "audit_log.json")
        data["events"][0]["payload"]["order_id"] = "CHANGED"
        (output / "audit_log.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.cli("audit", output / "audit_log.json").returncode, 2)

    def test_validate_does_not_create_outputs(self):
        result = self.cli("validate", "--input", self.input, "--config", self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "valid")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_existing_output_is_not_overwritten(self):
        output = self.folder / "existing"
        output.mkdir()
        sentinel = output / "notes.txt"
        sentinel.write_text("keep", encoding="utf-8")
        result = self.cli(
            "plan", "--input", self.input, "--config", self.config, "--output", output
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_invalid_input_has_error_code_and_no_partial_output(self):
        invalid = self.folder / "invalid.json"
        invalid.write_text('{"order": {}}', encoding="utf-8")
        output = self.folder / "plan"
        result = self.cli(
            "plan", "--input", invalid, "--config", self.config, "--output", output
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["code"], "VALIDATION_ERROR")
        self.assertFalse(output.exists())

    def test_duplicate_json_keys_and_non_finite_constants_are_rejected(self):
        file = self.folder / "input.json"
        for content in (
            '{"order": 1, "order": 2}',
            '{"amount": NaN}',
            '{"amount": Infinity}',
        ):
            file.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ValidationError):
                load_json(file)

    def test_csv_formula_like_identifiers_are_exported_as_text(self):
        scenario = load_scenario(self.input)
        scenario["materials"][2]["material_name"] = '=HYPERLINK("https://example.com")'
        outputs = KittingPlanner(load_json(self.config)).run(scenario)
        target = write_outputs(outputs, self.folder / "csv")
        with (target / "materials.csv").open(encoding="utf-8", newline="") as handle:
            row = next(
                row
                for row in csv.DictReader(handle)
                if row["material_id"] == "COMPONENT-HOUSING"
            )
        self.assertTrue(row["material_name"].startswith("'="))

    def test_json_directory_adapter_matches_combined_input(self):
        from bom_kitting_planner.engine import SCENARIO_FILES

        scenario = load_scenario(self.input)
        for key, name in SCENARIO_FILES.items():
            (self.folder / name).write_text(json.dumps(scenario[key]), encoding="utf-8")
        separate = load_scenario(self.folder)
        self.assertEqual(separate, {key: scenario[key] for key in SCENARIO_FILES})


if __name__ == "__main__":
    unittest.main()
