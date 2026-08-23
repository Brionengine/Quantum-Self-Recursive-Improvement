"""
QSRI — Quantum Self-Recursive Improvement
=========================================
Brion Quantum AI Team.

A self-improving search that tunes a target's configuration by measuring it, and
tunes its own search by measuring itself.

Three parts:

  QAIS  Quantum Amplitude Interference Search. A COMPLEX amplitude per gene
        value, selected by the Born rule. Phase lets settings that win TOGETHER
        rotate into alignment and reinforce constructively; settings that lose
        together cancel. A scalar weight per value cannot express "these two are
        only good in each other's company", and in real tuning spaces the knobs
        are strongly coupled.

  QMRA  Quantum Meta-Recursive Ascent. The recursion proper: the engine measures
        its own regret and tunes its OWN exploration temperature and
        interference strength. It improves the thing that does the improving.

  Amplitude-encoded selection. The learned distribution is loaded into a circuit
        with RY rotations so that P(measure i) == p_i, on a local simulator or on
        real QPU hardware. Measurement becomes a true draw from what was learned
        rather than from noise, and one shot suffices — which is what makes the
        hardware path affordable.

Everything is fail-closed. A failed build, a failed benchmark, a missing verdict
or a timeout yields INVALID — never a number, never a ranking. Correctness is
checked before speed, so the search cannot optimise into a fast-but-wrong result.
A ledger is written atomically after every trial, so improvement survives
restarts.

Lineage
-------
This is an update of Brion's own earlier self-recursive improvement work.
Two repositories are the direct ancestors:

  * QGACE — Quantum-Guided Autonomous Code Evolution
    (``Brion-Quantum-A.I.-General-System``). Contributes the improvement cycle
    — analyze, benchmark, transform, test, accept or roll back — together with
    per-strategy weights that evolve on historical success, atomic rollback and
    convergence detection.

  * QDSS — Quantum Demand Superposition Scaling (``quantum_auto_scaler``, which
    ships alongside QSRI in this repository). Contributes modelling a decision
    space as a superposition of states and collapsing it by measurement when a
    choice must be made.

QSRI is NOT descended from RecursiveSelfRegeneration: that is self-recursive
self-HEALING — regenerating degraded state — which is a different algorithm.

What this version changes
-------------------------
QGACE's quantum selection step built n Hadamards and nothing else, ran 100
shots, and took the most frequent outcome modulo the number of strategies.
Under uniform superposition that argmax is sampling noise, and
``strategy_weights`` — the entire learned state — never entered the circuit. So
whenever qiskit imported, the "quantum" path DISCARDED learning and did worse
than its own classical fallback. QSRI encodes the learned distribution into the
circuit, so measurement is a genuine draw from it. See :mod:`qsri.samplers`.
"""
from .engine import QAIS, QMRA, QSRI, Trial, Genome, INVALID, HARD_FAILURES
from .objectives import Objective, CommandObjective, SyntheticObjective

__all__ = ["QAIS", "QMRA", "QSRI", "Trial", "Genome", "INVALID",
           "HARD_FAILURES", "Objective", "CommandObjective",
           "SyntheticObjective", "__version__"]

__version__ = "2.0.0"
