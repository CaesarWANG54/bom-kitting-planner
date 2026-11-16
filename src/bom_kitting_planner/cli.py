"""Command line interface. No network or business-system writes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import KittingPlanner, load_json, load_scenario, verify_audit_chain
from .errors import PlannerError, ValidationError
from .reporting import write_outputs


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="bom-kitting-planner", description="BOM explosion and material planning"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan"):
        command = commands.add_parser(
            name,
            help="Check input and calculations"
            if name == "validate"
            else "Export a material plan",
        )
        command.add_argument(
            "--input",
            "-i",
            type=Path,
            required=True,
            help="Scenario JSON file or directory of JSON datasets",
        )
        command.add_argument(
            "--config", "-c", type=Path, required=True, help="Planning policy JSON file"
        )
        if name == "plan":
            command.add_argument(
                "--output", "-o", type=Path, required=True, help="New output directory"
            )
    audit = commands.add_parser(
        "audit", help="Check consistency of an exported audit hash chain"
    )
    audit.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            document = load_json(args.file)
            valid, sequence = verify_audit_chain(
                document.get("events") if isinstance(document, dict) else None
            )
            if not valid:
                raise ValidationError(
                    "审计链不完整或内容已改变",
                    details={"first_invalid_sequence": sequence},
                )
            result = {"status": "valid", "events": len(document["events"])}
        else:
            planner = KittingPlanner.from_config_file(args.config)
            outputs = planner.run(load_scenario(args.input))
            summary = outputs["run_summary"]
            result = {
                "status": "valid" if args.command == "validate" else "planned",
                "analysis_id": summary["analysis_id"],
                "shortage_materials": summary["residual_shortage_material_count"],
                "purchase_draft_lines": summary["purchase_requisition_draft_lines"],
            }
            if args.command == "plan":
                result["output"] = str(write_outputs(outputs, args.output))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except PlannerError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, UnicodeError) as error:
        print(
            json.dumps(
                {"code": "INPUT_OUTPUT_ERROR", "message": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return 2
