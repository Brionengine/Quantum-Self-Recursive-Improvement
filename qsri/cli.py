"""Command-line entry point: ``python -m qsri``."""
from __future__ import annotations

import argparse
import sys

from .engine import QSRI
from .objectives import CommandObjective, SyntheticObjective


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="qsri",
        description="QSRI - Quantum Self-Recursive Improvement")
    ap.add_argument("--spec", default=None,
                    help="JSON objective spec; see examples/gemm_tuning.json")
    ap.add_argument("--demo", action="store_true",
                    help="run against the synthetic landscape (no hardware)")
    ap.add_argument("--generations", type=int, default=12)
    ap.add_argument("--budget-hours", type=float, default=0.0,
                    help="wall-clock cap; 0 = no cap")
    ap.add_argument("--ledger", default="qsri_ledger.json")
    ap.add_argument("--workdir", default=None,
                    help="scratch directory for build and bench logs")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--qpu", default=None,
                    help="QPU backend for configuration draws, e.g. WK_C180")
    ap.add_argument("--no-quantum", action="store_true",
                    help="disable circuit-based sampling (classical only)")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.demo == bool(a.spec):
        print("qsri: give exactly one of --spec FILE or --demo", file=sys.stderr)
        return 2

    obj = (SyntheticObjective(break_rate=0.15)
           if a.demo else CommandObjective.from_file(a.spec, workdir=a.workdir))

    eng = QSRI(obj, ledger_path=a.ledger, seed=a.seed,
               quantum=not a.no_quantum, qpu=a.qpu)
    if eng.origin:
        print("QSRI: QPU = %s (%s)" % (
            eng.origin.backend,
            "ready" if eng.origin.available
            else "UNAVAILABLE: " + eng.origin.last_error))
    if eng.qsampler:
        print("QSRI: sampler backend = %s" % eng.qsampler.backend_name)
    if a.report_only:
        eng.report()
        return 0
    eng.run(a.generations, budget_seconds=a.budget_hours * 3600.0)
    return 0
