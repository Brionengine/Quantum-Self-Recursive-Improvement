"""
Engine tests: does QSRI find a coupled optimum, and does it stay fail-closed
when the objective breaks or lies?
"""
import math

import pytest

from qsri import QSRI, QAIS, QMRA, SyntheticObjective
from qsri.engine import Trial, INVALID


def _run(tmp_path, gens=60, **kw):
    obj = SyntheticObjective(**kw)
    eng = QSRI(obj, ledger_path=str(tmp_path / "ledger.json"), seed=42,
               quantum=False, quiet=True)
    eng.run(gens)
    return eng


def test_finds_the_coupled_optimum(tmp_path):
    """tma only pays off at STAGES=3; a scalar-weight searcher averages that
    away. The engine must end up believing in both together."""
    eng = _run(tmp_path)
    belief = eng.qais.collapse()
    assert belief["LOAD_POLICY"] == "tma"
    assert belief["STAGES"] == "3"
    matches = sum(1 for k, v in SyntheticObjective.OPTIMUM.items()
                  if belief.get(k) == v)
    assert matches >= 5, "belief matched only %d/7 genes" % matches
    assert eng.best.score / eng.baseline > 1.2


def test_survives_a_flaky_toolchain(tmp_path):
    """A 15% random build-failure rate must not erase what has been learned:
    a BUILD_FAIL says nothing about the configuration."""
    eng = _run(tmp_path, break_rate=0.15)
    belief = eng.qais.collapse()
    matches = sum(1 for k, v in SyntheticObjective.OPTIMUM.items()
                  if belief.get(k) == v)
    assert matches >= 4, "flaky builds collapsed belief to %d/7 genes" % matches


@pytest.mark.parametrize("kw", [{}, {"break_rate": 0.15},
                                {"lie_rate": 0.30}])
def test_fail_closed(tmp_path, kw):
    """No INVALID trial may ever carry a score, and no unvalidated
    configuration may ever be crowned best."""
    eng = _run(tmp_path, **kw)
    assert not [t for t in eng.trials if not t.valid and t.score is not None]
    assert eng.best is None or eng.best.valid


def test_a_lying_objective_is_never_scored(tmp_path):
    """Fast-but-wrong results arrive as VERDICT_FAIL and must stay unscored."""
    eng = _run(tmp_path, lie_rate=0.30)
    lied = [t for t in eng.trials if t.status == "VERDICT_FAIL"]
    assert lied, "the synthetic objective never lied - test is not exercising"
    assert all(t.score is INVALID for t in lied)


def test_qmra_tunes_itself(tmp_path):
    """The recursion must actually move: QMRA changes its own temperature."""
    eng = _run(tmp_path)
    assert eng.qmra.history, "QMRA never adapted"
    assert abs(eng.qmra.temperature - 1.0) > 1e-9


def test_ledger_resumes(tmp_path):
    """Improvement survives a restart: amplitudes are replayed from history."""
    led = str(tmp_path / "ledger.json")
    first = QSRI(SyntheticObjective(), ledger_path=led, seed=1,
                 quantum=False, quiet=True)
    first.run(20)
    second = QSRI(SyntheticObjective(), ledger_path=led, seed=1,
                  quantum=False, quiet=True)
    assert len(second.trials) == len(first.trials)
    assert second.baseline == first.baseline
    assert second.best.score == first.best.score
    assert second.qais.collapse() == first.qais.collapse()


def test_baseline_is_the_stock_config(tmp_path):
    eng = _run(tmp_path, gens=3)
    assert eng.trials[0].config == {g: v[0] for g, v
                                    in SyntheticObjective.GENOME.items()}


def test_an_invalid_baseline_stops_the_run(tmp_path):
    """Without a reference point there is nothing to compare to. Refuse."""
    class AlwaysBroken(SyntheticObjective):
        def evaluate(self, gen, config):
            return Trial(gen, config, INVALID, "BUILD_FAIL", "always", 0.0)

    eng = QSRI(AlwaysBroken(), ledger_path=str(tmp_path / "l.json"),
               quantum=False, quiet=True)
    eng.run(10)
    assert eng.baseline is None
    assert len(eng.trials) == 1


def test_amplitudes_stay_normalised():
    q = QAIS(SyntheticObjective.GENOME, seed=3)
    for _ in range(50):
        cfg = q.sample(1.0)
        q.reinforce(cfg, 0.4, 0.6)
        q.reinforce(cfg, -0.9, 0.6)
    for gene in q.genome:
        total = sum(p for _, p in q.probabilities(gene, 1.0))
        assert math.isclose(total, 1.0, rel_tol=1e-9)


def test_reinforce_never_annihilates_a_value():
    """A setting that looks bad now may be good beside a gene not yet tried."""
    q = QAIS({"G": ["a", "b"]}, seed=0)
    for _ in range(200):
        q.reinforce({"G": "a"}, -5.0, 1.2)
    p = dict(q.probabilities("G", 1.0))
    assert p["a"] > 0.0


def test_entropy_falls_as_belief_sharpens():
    q = QAIS(SyntheticObjective.GENOME, seed=5)
    start = q.entropy()
    for _ in range(60):
        q.reinforce(SyntheticObjective.OPTIMUM, 0.5, 0.8)
    assert q.entropy() < start


def test_qmra_explores_when_thrashing_and_exploits_when_winning():
    hot = QMRA()
    for _ in range(8):
        hot.observe(False)
    hot.adapt()
    assert hot.temperature > 1.0

    cold = QMRA()
    for _ in range(8):
        cold.observe(True)
    cold.adapt()
    assert cold.temperature < 1.0
