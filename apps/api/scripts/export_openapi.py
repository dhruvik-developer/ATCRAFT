"""Write the API's OpenAPI document to a file, deterministically.

The frontend's TypeScript types are GENERATED from this file, so it is the
contract between the two halves of the product rather than a by-product of them.
Master rules section 53: the API surface is defined once and consumed, never
guessed at.

Why this exists at all
----------------------
The frontend used to hand-write its types. ``tsc`` then passed while screens were
broken, because a hand-written type cannot disagree with the compiler - only with
the server. The ageing report changed shape and the ageing screen kept compiling
and stopped working. Generating from here makes that class of drift a build
failure instead of a support ticket.

Determinism matters
-------------------
Keys are sorted and the file ends with a newline, so re-running this on an
unchanged API produces a byte-identical file. That is what lets ``make
contract-check`` tell "the API changed" apart from "the serialiser felt
different today", and what keeps the committed contract reviewable as a diff.

Routers are mounted explicitly. ``src.main`` builds an app with no module routers
attached - they are added by :func:`register_module_routers`, which is *not*
idempotent, so it is called exactly once here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.response_contract import validate_response_contract

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = REPO_ROOT / "openapi.json"


def build_spec() -> dict:
    """Mount every module router and return the resulting OpenAPI document."""
    from src.api.router import register_module_routers

    mounted = register_module_routers()
    if not mounted:
        raise SystemExit(
            "No module routers mounted - the exported contract would be empty. "
            "This is a real failure, not an empty API: something is wrong with "
            "src.api.router.MODULE_ORDER or with the module imports."
        )

    from src.main import app

    spec = app.openapi()
    if not spec.get("paths"):
        raise SystemExit("OpenAPI document has no paths; refusing to write an empty contract.")
    try:
        validate_response_contract(spec)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return spec


def render(spec: dict) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to write the document (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "do not write; exit 1 if the file on disk differs from the API as it "
            "is now. This is the CI guard: it fails when someone changes an "
            "endpoint without regenerating the contract and the frontend types."
        ),
    )
    args = parser.parse_args(argv)

    rendered = render(build_spec())

    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist. Run: make contract", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"{args.output} is out of date with the running API.\n"
                "The backend's HTTP surface changed without the contract being "
                "regenerated, so the frontend's generated types no longer describe "
                "the server.\n"
                "Run:  make contract     (regenerates the contract and the TS types)",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} matches the API.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    paths = len(json.loads(rendered)["paths"])
    schemas = len(json.loads(rendered).get("components", {}).get("schemas", {}))
    print(f"Wrote {args.output} - {paths} endpoints, {schemas} schemas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
