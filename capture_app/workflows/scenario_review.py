"""Create or import a post-capture scenario annotation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from capture_app.storage.vault import SCENARIO_ANNOTATION_VERSION, VaultWriter


def annotation_template(session_id: str) -> dict:
    return {
        "schema_version": SCENARIO_ANNOTATION_VERSION,
        "scenario_id": f"{session_id}-scenario-1",
        "scenario_name": "",
        "intent": "",
        "actor_role": "",
        "environment": "",
        "fixture_alias": "",
        "starting_state": "",
        "selected_variants": {},
        "field_constraints": [
            {
                "field": "",
                "rule": "",
                "source": "curated",
            }
        ],
        "expected_assertions": [],
        "side_effects": [],
        "replay_risk": "unknown",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review a captured session by exporting or importing its scenario annotation"
    )
    parser.add_argument("--vault", default="./ObsidianVault")
    parser.add_argument("--session", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-template", metavar="PATH", help="write a JSON annotation template")
    group.add_argument("--annotation", metavar="PATH", help="validate and save a completed JSON annotation")
    args = parser.parse_args()

    vault = VaultWriter(Path(args.vault))
    try:
        if args.write_template:
            path = Path(args.write_template)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(annotation_template(args.session), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"Annotation template written to {path}")
            return

        path = Path(args.annotation)
        annotation = json.loads(path.read_text(encoding="utf-8"))
        saved = vault.save_scenario_annotation(args.session, annotation)
        print(f"Saved schema-v{SCENARIO_ANNOTATION_VERSION} annotation for {args.session}")
        print(f"Scenario: {saved.get('scenario_name') or saved.get('scenario_id')}")
    finally:
        vault.conn.close()


if __name__ == "__main__":
    main()
