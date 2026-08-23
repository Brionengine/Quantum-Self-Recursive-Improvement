"""
QSRI — Quantum Self-Recursive Improvement
=========================================
Brion Quantum AI Team.

Self-recursive improvement is the ability of an AI to improve ITSELF —
recursively, repeatedly, as an ability rather than an event. Each pass improves
the improver, so the next pass starts stronger and reaches further. That
compounding is the road to ASI.

The Brion Quantum AI Team built the original self-recursive improvement
algorithms. QSRI is the quantum realization of that work: the same recursion,
with the choosing carried by complex amplitudes and Born-rule measurement on
real quantum hardware.

The recursion has two levels, and the second is the one that matters:

  QAIS  Quantum Amplitude Interference Search — improves the TARGET.
        A COMPLEX amplitude per configuration value, selected by the Born rule.
        Phase lets settings that win TOGETHER rotate into alignment and
        reinforce constructively; settings that lose together cancel. A scalar
        weight per value cannot express "these two are only good in each other's
        company", and real decision spaces are strongly coupled.

  QMRA  Quantum Meta-Recursive Ascent — improves the IMPROVER.
        The engine measures its own regret and retunes its OWN exploration
        temperature and interference strength. Nothing about the loop is fixed
        in advance and no human retunes it. This is what makes the recursion
        self-sustaining rather than a single pass.

  Amplitude-encoded selection. The learned distribution is loaded into a circuit
        with RY rotations so that P(measure i) == p_i, on a local simulator or on
        real QPU hardware. Measurement becomes a true draw from what was learned
        rather than from noise, and one shot suffices — which is what makes the
        hardware path affordable.

The objective is deliberately pluggable. The loop is not ABOUT any particular
target; it is about a system that can measure itself against something and close
the gap on its own, repeatedly. Point it at a different objective and the same
recursion runs — nothing in the engine changes.

Everything is fail-closed. A failed evaluation, a missing verdict or a timeout
yields INVALID — never a number, never a ranking. A self-improving system that
cannot tell a real gain from a broken measurement does not improve; it drifts,
confidently. That is why the refusals come before the numbers. A ledger is
written atomically after every trial, so improvement survives restarts.

Lineage
-------
The original self-recursive improvement algorithms are the Brion Quantum AI
Team's own work. QSRI is the next version of ours, rebuilt on quantum
foundations. Two of our repositories are the direct ancestors:

  * QGACE — Quantum-Guided Autonomous Code Evolution
    (``Brion-Quantum-A.I.-General-System``). The self-improvement cycle itself:
    analyze, benchmark, transform, test, accept or roll back — with weights that
    evolve on historical success, atomic rollback and convergence detection.

  * QDSS — Quantum Demand Superposition Scaling (``quantum_auto_scaler``, which
    ships alongside QSRI in this repository). Modelling a decision space as a
    superposition and collapsing it by measurement when a choice must be made.

QSRI is NOT descended from RecursiveSelfRegeneration: that is self-recursive
self-HEALING — regenerating degraded state — which is a different algorithm.

What this version adds
----------------------
The second level, and real amplitudes.

QGACE improved a target and evolved scalar weights while doing it, but its loop
was fixed. QSRI makes the loop itself something the system measures and improves.

QGACE's quantum selection step also built n Hadamards and nothing else, ran 100
shots, and took the most frequent outcome modulo the number of strategies. Under
uniform superposition that argmax is sampling noise, and ``strategy_weights`` —
the entire learned state — never entered the circuit. So whenever qiskit
imported, the "quantum" path DISCARDED learning and did worse than its own
classical fallback. QSRI encodes the learned distribution into the circuit, so
measurement is a genuine draw from it. See :mod:`qsri.samplers`.
"""
from .engine import QAIS, QMRA, QSRI, Trial, Genome, INVALID, HARD_FAILURES
from .objectives import Objective, CommandObjective, SyntheticObjective

__all__ = ["QAIS", "QMRA", "QSRI", "Trial", "Genome", "INVALID",
           "HARD_FAILURES", "Objective", "CommandObjective",
           "SyntheticObjective", "__version__"]

__version__ = "2.0.0"
