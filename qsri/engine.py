"""
QSRI engine — QAIS amplitude search, QMRA meta-recursion, and the six-phase loop.

The engine is objective-agnostic. It knows how to propose a configuration, how
to interpret a verdict, and how to improve its own search; it knows nothing
about what is being optimised. Everything domain-specific lives behind the
Objective protocol in :mod:`qsri.objectives`.
"""
from __future__ import annotations

import cmath
import json
import math
import os
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

INVALID = None  # explicit sentinel: "we do not have a number", not "zero"

# A genome maps a knob name to the list of values it may take. By convention the
# FIRST value of every gene is the stock setting, so taking element 0 of every
# gene reproduces an untuned build — that is how the baseline is measured.
Genome = Dict[str, List[str]]


@dataclass
class Trial:
    """One evaluated configuration. score is None when the trial is INVALID."""
    gen: int
    config: Dict[str, str]
    score: Optional[float]          # higher is better; None means no number
    status: str                     # OK | BUILD_FAIL | BENCH_FAIL | VERDICT_FAIL
                                    # | IMPLAUSIBLE | TIMEOUT
    detail: str = ""
    seconds: float = 0.0
    stamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    @property
    def valid(self) -> bool:
        return self.status == "OK" and self.score is not None


# Statuses that are the CONFIGURATION's fault: deterministic, reproducible, and
# therefore real information about the landscape. Everything else (a flaky
# toolchain, a transient timeout) says nothing about the configuration and must
# not be learned from as if it did.
HARD_FAILURES = ("VERDICT_FAIL", "IMPLAUSIBLE")


# ---------------------------------------------------------------------------
# QAIS — Quantum Amplitude Interference Search
# ---------------------------------------------------------------------------
class QAIS:
    """
    Complex-amplitude state over gene values.

    ``state[gene][value]`` is a complex amplitude. P(value) = |amp|^2 normalised
    across that gene. Feedback rotates phase as well as scaling magnitude:

      - a winning configuration rotates every gene value it used toward a shared
        reference phase, so those values interfere CONSTRUCTIVELY next draw;
      - a losing configuration rotates its values away, so they cancel.

    Coupled settings therefore rise and fall together. A scalar weight per value
    — the usual formulation — cannot express "these two settings are only good
    in each other's company", and in real tuning spaces the knobs are strongly
    coupled, so that expressiveness is the entire point.
    """

    #: Floor on the per-step magnitude multiplier applied by reinforce().
    AMP_GAIN_FLOOR = 0.15

    def __init__(self, genome: Genome, seed: Optional[int] = None):
        self.genome = genome
        self.rng = random.Random(seed)
        # Equal superposition: every value equally likely, phases aligned at 0.
        self.state: Dict[str, Dict[str, complex]] = {
            g: {v: complex(1.0, 0.0) for v in vals} for g, vals in genome.items()
        }

    def probabilities(self, gene: str, temperature: float) -> List[Tuple[str, float]]:
        amps = self.state[gene]
        mags = {v: abs(a) ** 2 for v, a in amps.items()}
        # Temperature flattens (explore) or sharpens (exploit) the Born
        # distribution. T -> inf is uniform; T -> 0 is argmax.
        t = max(temperature, 1e-6)
        adj = {v: m ** (1.0 / t) for v, m in mags.items()}
        total = sum(adj.values()) or 1.0
        return [(v, p / total) for v, p in adj.items()]

    def sample(self, temperature: float, quantum: Any = None) -> Dict[str, str]:
        """Draw one configuration, gene by gene, optionally via a real circuit."""
        cfg = {}
        for gene in self.genome:
            dist = self.probabilities(gene, temperature)
            if quantum is not None:
                choice = quantum.draw(dist)
                if choice is not None:
                    cfg[gene] = choice
                    continue
            r, acc = self.rng.random(), 0.0
            cfg[gene] = dist[-1][0]
            for value, p in dist:
                acc += p
                if r <= acc:
                    cfg[gene] = value
                    break
        return cfg

    def reinforce(self, config: Dict[str, str], advantage: float, strength: float):
        """
        advantage > 0 : config beat the running mean -> constructive rotation.
        advantage < 0 : it lost -> destructive rotation.

        Magnitude scales with |advantage| so a marginal win nudges and a large
        win commits. Phase carries the coupling: all genes in one configuration
        rotate by the same angle, which is what makes them reinforce jointly.
        """
        theta = strength * math.tanh(advantage) * (math.pi / 4.0)
        gain = 1.0 + strength * math.tanh(advantage)
        gain = max(gain, self.AMP_GAIN_FLOOR)   # never annihilate a value
                                        # outright: a setting that looks bad now
                                        # may be good alongside a gene not yet
                                        # tried. _renormalise enforces the same
                                        # invariant against numeric underflow.
        for gene, value in config.items():
            if gene not in self.state or value not in self.state[gene]:
                continue
            self.state[gene][value] = self.state[gene][value] * cmath.exp(1j * theta) * gain
        self._renormalise()

    #: No value's amplitude may fall below this. Repeated maximal punishment
    #: multiplies magnitude by AMP_GAIN_FLOOR each time, which underflows to
    #: exactly 0.0 within a few hundred trials - and a zero amplitude is
    #: unrecoverable: no later evidence can ever raise it again. The floor keeps
    #: probability strictly positive, so a setting that looks bad now can still
    #: come back when it is finally tried beside the right partner.
    AMP_FLOOR = 1e-9

    def _renormalise(self):
        for _gene, amps in self.state.items():
            norm = math.sqrt(sum(abs(a) ** 2 for a in amps.values())) or 1.0
            floored = False
            for v, a in amps.items():
                a = a / norm
                if abs(a) < self.AMP_FLOOR:
                    # Preserve phase where there is one; a true zero has none.
                    phase = a / abs(a) if abs(a) > 0 else complex(1.0, 0.0)
                    a = phase * self.AMP_FLOOR
                    floored = True
                amps[v] = a
            if floored:
                norm = math.sqrt(sum(abs(a) ** 2 for a in amps.values())) or 1.0
                for v in amps:
                    amps[v] /= norm

    def collapse(self) -> Dict[str, str]:
        """Most probable value per gene — the engine's current best belief."""
        return {g: max(a, key=lambda v: abs(a[v]) ** 2) for g, a in self.state.items()}

    def entropy(self) -> float:
        """Mean Shannon entropy (bits) over genes: how undecided we still are."""
        out = []
        for gene in self.state:
            ps = [p for _, p in self.probabilities(gene, 1.0) if p > 0]
            out.append(-sum(p * math.log2(p) for p in ps))
        return sum(out) / len(out) if out else 0.0

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        return {g: {v: round(abs(a) ** 2, 4) for v, a in amps.items()}
                for g, amps in self.state.items()}


# ---------------------------------------------------------------------------
# QMRA — Quantum Meta-Recursive Ascent (the self-recursive layer)
# ---------------------------------------------------------------------------
class QMRA:
    """
    Tunes QSRI's own search hyperparameters from its own measured regret.

    Regret here = fraction of recent trials that failed to beat the running
    mean. High regret means the search is thrashing: raise temperature to
    explore and soften interference so it stops committing to a bad basin. Low
    regret means it is on a productive slope: cool down and sharpen.

    This is the recursion. Phases 1-6 improve the target; QMRA improves
    phases 1-6.
    """

    def __init__(self, temperature: float = 1.0, interference: float = 0.6):
        self.temperature = temperature
        self.interference = interference
        self.window: List[bool] = []          # True = trial was above average
        self.history: List[Dict[str, float]] = []

    def observe(self, improved: bool, window: int = 8):
        self.window.append(improved)
        if len(self.window) > window:
            self.window.pop(0)

    def adapt(self) -> Dict[str, float]:
        if len(self.window) < 4:
            return {"temperature": self.temperature,
                    "interference": self.interference, "regret": float("nan")}
        regret = 1.0 - (sum(self.window) / len(self.window))
        if regret > 0.85:
            # Thrashing: nothing is working. Explore harder, commit less.
            self.temperature = min(self.temperature * 1.35, 4.0)
            self.interference = max(self.interference * 0.85, 0.15)
        elif regret < 0.5:
            # Finding wins: exploit the slope we are on.
            self.temperature = max(self.temperature * 0.8, 0.25)
            self.interference = min(self.interference * 1.15, 1.2)
        rec = {"temperature": round(self.temperature, 4),
               "interference": round(self.interference, 4),
               "regret": round(regret, 4)}
        self.history.append(rec)
        return rec


# ---------------------------------------------------------------------------
# The QSRI engine — six phases
# ---------------------------------------------------------------------------
class QSRI:
    """
    OBSERVE -> ANALYZE -> STRATEGIZE -> APPLY -> EVALUATE -> EVOLVE, with QMRA
    closing the recursion by tuning the loop that runs it.

    The genome is taken from the objective, so pointing QSRI at a different
    target is a one-line change and never touches the engine.
    """

    def __init__(self, objective, ledger_path: str = "qsri_ledger.json",
                 seed: Optional[int] = None, quantum: bool = True,
                 qpu: Optional[str] = None, quiet: bool = False):
        self.objective = objective
        self.ledger_path = ledger_path
        self.quiet = quiet
        self.qais = QAIS(objective.genome, seed=seed)
        self.qmra = QMRA()
        self.qsampler = None
        self.origin = None
        if quantum:
            from .samplers import QuantumSampler
            self.qsampler = QuantumSampler()
        if qpu:
            # Real QPU hardware, when asked for. Falls through to the simulator
            # and then to classical on any failure — never fabricates.
            from .samplers import OriginSampler
            self.origin = OriginSampler(backend=qpu)
        self.trials: List[Trial] = []
        self.best: Optional[Trial] = None
        self.baseline: Optional[float] = None
        self._load()

    def _say(self, msg: str = ""):
        if not self.quiet:
            print(msg)

    # -- persistence: recursion across restarts ------------------------------
    def _load(self):
        if not os.path.exists(self.ledger_path):
            return
        try:
            with open(self.ledger_path) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            self._say("QSRI: ledger unreadable (%s) - starting fresh" % e)
            return
        known = set(Trial.__dataclass_fields__)
        self.trials = [Trial(**{k: v for k, v in t.items() if k in known})
                       for t in d.get("trials", [])]
        self.baseline = d.get("baseline")
        self.objective.baseline = self.baseline
        self.objective.baseline_secondary = d.get("baseline_secondary")
        self.qmra.temperature = d.get("temperature", 1.0)
        self.qmra.interference = d.get("interference", 0.6)
        # Replay history so amplitudes reflect everything ever learned.
        valid = [t for t in self.trials if t.valid]
        if valid:
            self.best = max(valid, key=lambda t: t.score)
            mean = sum(t.score for t in valid) / len(valid)
            for t in self.trials:
                adv = ((t.score - mean) / mean) if t.valid else (
                    -1.0 if t.status in HARD_FAILURES else -0.25)
                self.qais.reinforce(t.config, adv, self.qmra.interference)
        self._say("QSRI: resumed from %d trials (%d valid), best=%s"
                  % (len(self.trials), len(valid),
                     ("%.4g" % self.best.score) if self.best else "none"))

    def _save(self):
        tmp = self.ledger_path + ".tmp"
        payload = {"trials": [asdict(t) for t in self.trials],
                   "baseline": self.baseline,
                   "baseline_secondary": getattr(self.objective,
                                                 "baseline_secondary", None),
                   "temperature": self.qmra.temperature,
                   "interference": self.qmra.interference,
                   "amplitudes": self.qais.snapshot(),
                   "belief": self.qais.collapse()}
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.ledger_path)   # atomic: a killed run cannot
                                            # leave a truncated ledger

    def stock_config(self) -> Dict[str, str]:
        """Element 0 of every gene: the untuned reference build."""
        return {g: vals[0] for g, vals in self.qais.genome.items()}

    # -- the loop ------------------------------------------------------------
    def run(self, generations: int, budget_seconds: float = 0.0):
        started = time.time()

        # Phase 1 OBSERVE — establish the honest baseline (stock config) once.
        if self.baseline is None:
            self._say("\n[QSRI] OBSERVE - measuring stock baseline")
            t = self.objective.evaluate(len(self.trials), self.stock_config())
            self.trials.append(t)
            if not t.valid:
                self._say("  baseline INVALID (%s: %s) - cannot proceed without "
                          "a reference point" % (t.status, t.detail))
                self._save()
                return
            self.baseline = t.score
            self.objective.baseline = t.score
            self.objective.observe_baseline(t)
            self.best = t
            self._say("  baseline = %.6g %s  (%s)"
                      % (t.score, self.objective.units, t.detail))
            self._save()

        for _ in range(generations):
            if budget_seconds and (time.time() - started) > budget_seconds:
                self._say("\n[QSRI] budget exhausted - stopping cleanly")
                break
            gen = len(self.trials)

            # Phase 2 ANALYZE + Phase 3 STRATEGIZE
            meta = self.qmra.adapt()
            cfg = None
            if self.origin is not None:
                dists = {g: self.qais.probabilities(g, self.qmra.temperature)
                         for g in self.qais.genome}
                cfg = self.origin.draw_config(dists)
                if cfg is None and self.origin.last_error:
                    self._say("  QPU unavailable (%s) - falling back"
                              % self.origin.last_error[:70])
            if cfg is None:
                cfg = self.qais.sample(self.qmra.temperature, self.qsampler)
            else:
                self._say("  config drawn on %s (job %d)"
                          % (self.origin.backend, self.origin.jobs))
            regret = meta["regret"]
            self._say("\n[QSRI] gen %d  T=%.3f  I=%.3f  regret=%s  H=%.3f bits"
                      % (gen, meta["temperature"], meta["interference"],
                         ("%.2f" % regret) if regret == regret else "n/a",
                         self.qais.entropy()))
            self._say("  try: " + " ".join("%s=%s" % (k, v)
                                           for k, v in sorted(cfg.items())))

            # Phase 4 APPLY + Phase 5 EVALUATE (fail-closed)
            t = self.objective.evaluate(gen, cfg)
            self.trials.append(t)

            if not t.valid:
                self._say("  INVALID (%s) %s  [%.0fs]"
                          % (t.status, t.detail, t.seconds))
                # A correctness failure is the configuration's fault and must be
                # punished hard. A build or bench failure may be a flaky
                # toolchain, and hammering the genes for that erases real
                # knowledge - under a 15% random build-failure rate the
                # self-test showed belief collapsing from 6/7 to 2/7 genes.
                # Penalise it, but softly enough that a genuinely bad
                # combination still sinks over repeats.
                penalty = -1.0 if t.status in HARD_FAILURES else -0.25
                self.qais.reinforce(cfg, penalty, self.qmra.interference)
                # QMRA asks "am I finding good configurations?". A toolchain
                # flake answers nothing about the landscape, so it must not
                # count as regret - letting it do so pinned temperature at the
                # 4.0 cap under a 15% failure rate, i.e. permanent random
                # search. A correctness failure IS landscape information.
                if t.status in HARD_FAILURES:
                    self.qmra.observe(False)
                self._save()
                continue

            rel = t.score / self.baseline if self.baseline else float("nan")
            is_best = (self.best is None) or (t.score > self.best.score)
            self._say("  %.6g %s = %.4fx baseline  %s  [%.0fs]"
                      % (t.score, self.objective.units, rel,
                         "NEW BEST" if is_best else "", t.seconds))

            # Phase 6 EVOLVE
            valid = [x for x in self.trials if x.valid]
            mean = sum(x.score for x in valid) / len(valid)
            self.qais.reinforce(cfg, (t.score - mean) / mean,
                                self.qmra.interference)
            # QMRA's success signal is "was this draw above average", NOT "did
            # it beat the all-time best". The latter becomes impossible once we
            # are near the optimum, driving regret to 1.0 and pinning
            # temperature at max - the engine would explore randomly forever
            # and never exploit.
            self.qmra.observe(t.score > mean)
            if is_best:
                self.best = t
            self._save()

        self.report()

    def report(self):
        valid = [t for t in self.trials if t.valid]
        self._say("\n" + "=" * 68)
        self._say("QSRI REPORT - %d trials, %d valid, %d invalid"
                  % (len(self.trials), len(valid), len(self.trials) - len(valid)))
        if self.baseline:
            self._say("baseline      : %.6g %s" % (self.baseline, self.objective.units))
        if self.best and self.baseline:
            self._say("best measured : %.6g %s = %.4fx"
                      % (self.best.score, self.objective.units,
                         self.best.score / self.baseline))
            self._say("best config   :")
            for k, v in sorted(self.best.config.items()):
                self._say("    %s=%s" % (k, v))
        bad: Dict[str, int] = {}
        for t in self.trials:
            if not t.valid:
                bad[t.status] = bad.get(t.status, 0) + 1
        if bad:
            self._say("invalid by cause: "
                      + ", ".join("%s=%d" % kv for kv in sorted(bad.items())))
        self._say("belief (QAIS collapse): "
                  + " ".join("%s=%s" % (k, v)
                             for k, v in sorted(self.qais.collapse().items())))
        self._say("=" * 68)
