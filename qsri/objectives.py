"""
Objectives — what QSRI is pointed at.

An objective owns three things and nothing else:

  * ``genome``  - the knobs and their allowed values (element 0 = stock),
  * ``units``   - what the score is measured in, for reporting,
  * ``evaluate`` - turn one configuration into a :class:`~qsri.engine.Trial`.

Everything domain-specific lives here, so retargeting QSRI never touches the
engine. Two implementations ship:

  * :class:`CommandObjective`   - build and benchmark anything a shell can run,
    described entirely by a JSON spec.
  * :class:`SyntheticObjective` - an analytic landscape with a known optimum and
    a deliberate two-gene coupling, used by the tests and by ``--demo``.

Every objective must be FAIL-CLOSED. A failed build, a failed benchmark, a
missing verdict or a timeout yields INVALID - never a number, never a ranking.
Correctness is checked BEFORE speed, so the search cannot optimise its way into
a fast-but-wrong result.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from .engine import Genome, Trial, INVALID


class Objective:
    """Interface every objective implements."""

    #: knob -> allowed values; element 0 of each gene is the stock setting
    genome: Genome = {}
    #: label for the score in reports, e.g. "ops/s"
    units: str = "score"
    #: filled in by the engine after the stock configuration is measured
    baseline: Optional[float] = None
    #: optional second metric, used by plausibility gates
    baseline_secondary: Optional[float] = None

    def observe_baseline(self, trial: Trial) -> None:
        """Hook called once, with the stock trial, after the baseline is set."""

    def evaluate(self, gen: int, config: Dict[str, str]) -> Trial:
        raise NotImplementedError


class CommandObjective(Objective):
    """
    Build-and-benchmark any target described by a JSON spec.

    The configuration is passed to both commands as environment variables (a
    gene whose value is the empty string is UNSET rather than set to "", so the
    target's own default applies - that is what makes element 0 a true stock
    build). The same assignments are also available to the command templates as
    ``{env}`` for make-style invocation, along with ``{workdir}`` and ``{tag}``.

    Spec keys::

        genome            {gene: [values...]}          required
        build             shell command                 optional
        bench             shell command                 required
        score_regex       first group -> float          required
        higher_is_better  default true
        verdict_regex     first group compared to verdict_ok
        verdict_ok        default "OK"
        secondary_regex   optional second metric, for the plausibility gate
        plausible_max     reject score > baseline * this
        plausible_min_secondary   reject secondary < baseline_secondary * this
        build_timeout     seconds, default 1800
        bench_timeout     seconds, default 600
        workdir           scratch directory, default ./qsri_work
        lock_file         serialise builds that share one tree
        units             label for reports

    On a shared build tree set ``lock_file``. Two concurrent builds in one tree
    run each other's ``clean`` and delete each other's objects, producing a make
    that exits 0 having built nothing - which then surfaces downstream as a
    confusing missing-artifact error. A lock makes that failure mode impossible
    rather than merely unlikely.
    """

    def __init__(self, spec: Dict, workdir: Optional[str] = None):
        self.spec = spec
        self.genome = spec["genome"]
        if not self.genome:
            raise ValueError("spec.genome is empty")
        for gene, values in self.genome.items():
            if not values:
                raise ValueError("gene %r has no values" % gene)
        self.units = spec.get("units", "score")
        self.build_cmd = spec.get("build") or ""
        self.bench_cmd = spec["bench"]
        self.score_re = re.compile(spec["score_regex"])
        self.higher_is_better = bool(spec.get("higher_is_better", True))
        self.verdict_re = (re.compile(spec["verdict_regex"])
                           if spec.get("verdict_regex") else None)
        self.verdict_ok = str(spec.get("verdict_ok", "OK")).upper()
        self.secondary_re = (re.compile(spec["secondary_regex"])
                             if spec.get("secondary_regex") else None)
        self.plausible_max = spec.get("plausible_max")
        self.plausible_min_secondary = spec.get("plausible_min_secondary")
        self.build_timeout = int(spec.get("build_timeout", 1800))
        self.bench_timeout = int(spec.get("bench_timeout", 600))
        self.lock_file = spec.get("lock_file")
        self.workdir = os.path.abspath(
            workdir or spec.get("workdir") or "qsri_work")
        os.makedirs(self.workdir, exist_ok=True)
        self.baseline: Optional[float] = None
        self.baseline_secondary: Optional[float] = None
        if not self.higher_is_better:
            self.units = "1/(%s)" % self.units

    @classmethod
    def from_file(cls, path: str, workdir: Optional[str] = None) -> "CommandObjective":
        with open(path) as fh:
            return cls(json.load(fh), workdir=workdir)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _env_assignments(config: Dict[str, str]) -> str:
        return " ".join("%s=%s" % (k, v) for k, v in sorted(config.items()) if v != "")

    def _run(self, cmd: str, timeout: int, config: Dict[str, str]) -> Tuple[int, str]:
        env = dict(os.environ)
        for k, v in config.items():
            if v == "":
                env.pop(k, None)      # unset -> the target's own default applies
            else:
                env[k] = v
        if self.lock_file:
            # flock(1) around the whole command, not just make: the clean step
            # is part of what must not interleave.
            cmd = ("exec 9>%s; flock -w %d 9 || exit 75; %s"
                   % (self.lock_file, timeout, cmd))
        # pipefail matters: `make | tail` reports TAIL's exit status, so a
        # failed build would look successful and only surface later as a
        # confusing missing-artifact error. Keep the real compiler error.
        cmd = "set -o pipefail; " + cmd
        try:
            p = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                               text=True, timeout=timeout, env=env)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "TIMEOUT"

    def _format(self, cmd: str, config: Dict[str, str], tag: str) -> str:
        # Explicit replacement, NOT str.format: shell commands are full of
        # braces - ``${VAR:-default}``, awk programs, brace expansion - and
        # str.format raises KeyError on every one of them.
        for token, value in (("{env}", self._env_assignments(config)),
                             ("{workdir}", self.workdir),
                             ("{tag}", tag)):
            cmd = cmd.replace(token, value)
        return cmd

    def _save_log(self, tag: str, kind: str, rc: int,
                  config: Dict[str, str], out: str) -> str:
        # Keep the WHOLE log. The last line of a failed build is usually a
        # downstream symptom rather than the compiler error that caused it, and
        # diagnosing from the symptom wastes real time.
        path = os.path.join(self.workdir, "%s.%s.log" % (tag, kind))
        try:
            with open(path, "w") as fh:
                fh.write("rc=%d\nenv: %s\n\n%s"
                         % (rc, self._env_assignments(config), out))
        except OSError:
            return "(unwritable)"
        return path

    @staticmethod
    def _first_error(out: str) -> str:
        lines = [l for l in out.strip().splitlines() if l.strip()]
        if not lines:
            return ""
        hit = next((l for l in reversed(lines)
                    if re.search(r"error|Error|\*\*\*", l)), lines[-1])
        return hit[:140]

    def observe_baseline(self, trial: Trial) -> None:
        m = re.search(r"secondary=([0-9.eE+-]+)", trial.detail or "")
        if m:
            self.baseline_secondary = float(m.group(1))

    # -- the evaluation ------------------------------------------------------
    def evaluate(self, gen: int, config: Dict[str, str]) -> Trial:
        t0 = time.time()
        tag = "g%04d" % gen

        if self.build_cmd:
            rc, out = self._run(self._format(self.build_cmd, config, tag),
                                self.build_timeout, config)
            if rc != 0:
                status = "TIMEOUT" if rc == 124 else "BUILD_FAIL"
                log = self._save_log(tag, "build", rc, config, out)
                return Trial(gen, config, INVALID, status,
                             "%s | log=%s" % (self._first_error(out), log),
                             time.time() - t0)

        rc, out = self._run(self._format(self.bench_cmd, config, tag),
                            self.bench_timeout, config)
        if rc == 124:
            return Trial(gen, config, INVALID, "TIMEOUT", "bench timeout",
                         time.time() - t0)
        if rc != 0:
            log = self._save_log(tag, "bench", rc, config, out)
            return Trial(gen, config, INVALID, "BENCH_FAIL",
                         "%s | log=%s" % (self._first_error(out), log),
                         time.time() - t0)

        # Correctness gate FIRST. A fast result that fails its verdict is not a
        # faster result, it is a broken one. A configured verdict that does not
        # appear in the output is a FAILURE, not a pass: a check that cannot say
        # "no" cannot be trusted when it says nothing.
        if self.verdict_re is not None:
            m = self.verdict_re.search(out)
            if not m:
                return Trial(gen, config, INVALID, "VERDICT_FAIL",
                             "no verdict in benchmark output", time.time() - t0)
            got = m.group(1).upper()
            if got != self.verdict_ok:
                return Trial(gen, config, INVALID, "VERDICT_FAIL",
                             "verdict=%s" % got, time.time() - t0)

        m = self.score_re.search(out)
        if not m:
            log = self._save_log(tag, "bench", rc, config, out)
            return Trial(gen, config, INVALID, "BENCH_FAIL",
                         "score_regex did not match | log=%s" % log,
                         time.time() - t0)
        try:
            raw = float(m.group(1))
        except ValueError:
            return Trial(gen, config, INVALID, "BENCH_FAIL",
                         "score %r is not a number" % m.group(1), time.time() - t0)
        if not math.isfinite(raw) or raw <= 0:
            return Trial(gen, config, INVALID, "BENCH_FAIL",
                         "score %r is not usable" % raw, time.time() - t0)
        # The engine maximises. A lower-is-better metric is inverted here, and
        # the raw value is kept in `detail` so reports stay readable.
        score = raw if self.higher_is_better else 1.0 / raw

        detail = "raw=%.6g" % raw
        secondary = None
        if self.secondary_re is not None:
            s = self.secondary_re.search(out)
            if s:
                try:
                    secondary = float(s.group(1))
                    detail += " secondary=%.6g" % secondary
                except ValueError:
                    secondary = None

        # ---- PLAUSIBILITY GATE ------------------------------------------
        # A verdict that only checks self-consistency is not a correctness
        # check: a target that silently does NOTHING can pass one and report an
        # absurd speedup. Until a bit-exactness check exists, bound the result
        # physically. A configured bound that a real win cannot approach costs
        # nothing and refuses the broken cases outright, rather than letting the
        # search spend its whole budget chasing them.
        if self.baseline and self.plausible_max:
            if score > self.baseline * float(self.plausible_max):
                return Trial(gen, config, INVALID, "IMPLAUSIBLE",
                             "%.6g = %.1fx baseline exceeds the configured "
                             "bound (%s)" % (score, score / self.baseline, detail),
                             time.time() - t0)
        if (secondary is not None and self.baseline_secondary
                and self.plausible_min_secondary):
            floor = self.baseline_secondary * float(self.plausible_min_secondary)
            if secondary < floor:
                return Trial(gen, config, INVALID, "IMPLAUSIBLE",
                             "secondary %.6g below floor %.6g - too fast to be "
                             "real work" % (secondary, floor), time.time() - t0)
        return Trial(gen, config, score, "OK", detail, time.time() - t0)


class SyntheticObjective(Objective):
    """
    An analytic landscape with a known optimum, used by the tests and ``--demo``.

    It exists to answer two questions without any hardware: can the engine find
    an optimum that is only reachable through a COUPLING between two genes, and
    does it stay fail-closed when the objective breaks or lies?

    ``break_rate`` injects non-deterministic BUILD_FAILs (a flaky toolchain).
    ``lie_rate`` injects fast-but-wrong results that must be refused on the
    verdict, never scored.
    """

    GENOME: Genome = {
        "LOAD_POLICY":   ["cp_async", "tma"],
        "MANUAL_MMA":    ["", "0", "1"],
        "XOR_ACCUMS":    ["", "4", "8", "16"],
        "SWIZZLE_BITS":  ["", "2", "3"],
        "STAGES":        ["", "2", "3"],
        "KBLOCK":        ["", "64", "128"],
        "MIN_BLOCKS":    ["", "1", "2"],
    }

    #: The optimum the engine is expected to find.
    OPTIMUM = {"LOAD_POLICY": "tma", "MANUAL_MMA": "1", "XOR_ACCUMS": "4",
               "SWIZZLE_BITS": "3", "STAGES": "3", "KBLOCK": "128",
               "MIN_BLOCKS": "1"}

    units = "ops/s"

    def __init__(self, break_rate: float = 0.0, lie_rate: float = 0.0,
                 noise: float = 0.01, seed: int = 7):
        self.genome = dict(self.GENOME)
        self.break_rate = break_rate
        self.lie_rate = lie_rate
        self.noise = noise
        self.rng = random.Random(seed)
        self.calls = 0
        self.baseline = None
        self.baseline_secondary = None

    def evaluate(self, gen: int, config: Dict[str, str]) -> Trial:
        self.calls += 1
        if self.rng.random() < self.break_rate:
            return Trial(gen, config, INVALID, "BUILD_FAIL",
                         "synthetic build failure", 0.01)
        if self.rng.random() < self.lie_rate:
            # Fast but WRONG. Must be refused on the verdict, never scored.
            return Trial(gen, config, INVALID, "VERDICT_FAIL",
                         "verdict=MISMATCH", 0.01)
        hits = sum(1 for k, v in self.OPTIMUM.items() if config.get(k) == v)
        score = 1.0e9 * (1 + 0.05 * hits)
        # The coupling: tma only pays off at STAGES=3. A searcher carrying one
        # scalar weight per value tends to average this away.
        if config.get("LOAD_POLICY") == "tma" and config.get("STAGES") == "3":
            score *= 1.25
        score *= (1 + self.rng.gauss(0, self.noise))
        return Trial(gen, config, score, "OK", "synthetic", 0.01)
