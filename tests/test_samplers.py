"""
Amplitude-encoding tests. These are the ones that catch the two errors the
design exists to avoid: encoding a distribution and then not sampling from it,
and transposing basis states through an endianness mismatch.
"""
import math

import pytest

from qsri.samplers import _tree_angles, _weighted_pick, QuantumSampler

qiskit = pytest.importorskip("qiskit", reason="qiskit not installed")
pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")


TARGET = [("a", 0.50), ("b", 0.25), ("c", 0.15), ("d", 0.10)]
DRAWS = 2000


@pytest.fixture(scope="module")
def measured():
    """Measure the encoded distribution once; two assertions read it.

    Each draw is a fresh one-shot circuit, which is the thing under test - so
    this is deliberately empirical, and shared across tests to stay quick.
    """
    s = QuantumSampler(shots=1)
    assert s.backend_name == "qiskit-aer"
    seen = {v: 0 for v, _ in TARGET}
    for _ in range(DRAWS):
        got = s.draw(TARGET)
        assert got is not None, "sampler fell back mid-run"
        seen[got] += 1
    return {v: c / DRAWS for v, c in seen.items()}


def test_tree_angles_encode_a_uniform_distribution():
    angles = _tree_angles([0.25] * 4, 2)
    # Every split is even, so every angle is 2*arccos(sqrt(1/2)) = pi/2.
    for _lvl, _node, theta in angles:
        assert math.isclose(theta, math.pi / 2, rel_tol=1e-9)


def test_measurement_matches_the_encoded_distribution(measured):
    """P(measure i) must equal p_i. Argmax-of-shots would fail this outright."""
    for value, p in TARGET:
        assert abs(measured[value] - p) < 0.04, \
            "%s: %.3f vs %.3f" % (value, measured[value], p)


def test_endianness_is_not_transposed(measured):
    """The middle outcomes are where a little-endian slip hides: the endpoints
    look right while b and c swap."""
    assert measured["b"] > measured["c"], \
        "b and c are transposed: %r" % measured


def test_a_degenerate_distribution_falls_back_rather_than_guessing():
    assert QuantumSampler().draw([("only", 1.0)]) is None


def test_weighted_pick_is_a_draw_not_an_argmax():
    counts = {"0": 90, "1": 10}
    picks = [_weighted_pick(counts) for _ in range(3000)]
    share = picks.count("1") / len(picks)
    assert 0.06 < share < 0.14, "argmax collapse: minority share %.3f" % share
