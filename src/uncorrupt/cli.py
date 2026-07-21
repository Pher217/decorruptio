"""`uncorrupt` CLI: run connector, compute indicators, export, validate-registry.

Thin wrapper; the pipeline spine is Dagster (uncorrupt.pipelines).
"""

from __future__ import annotations

import argparse

from uncorrupt.register.loader import all_sources


def _validate_registry(_: argparse.Namespace) -> int:
    sources = all_sources()
    print(f"OK: {len(sources)} valid source register entries")
    for s in sources:
        print(
            f"  - {s.source_id}: {s.data_class.value}/tier-{s.tier.value} "
            f"[{s.redistribution.value}]"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uncorrupt")
    sub = parser.add_subparsers(dest="cmd", required=True)
    vr = sub.add_parser("validate-registry", help="load + validate sources/*.yml")
    vr.set_defaults(func=_validate_registry)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
