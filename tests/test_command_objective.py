"""
CommandObjective tests. These run real shell commands, so they exercise the
actual subprocess, environment and regex path rather than a mock of it.
"""
import json

import pytest

from qsri.engine import INVALID
from qsri.objectives import CommandObjective

GENOME = {"KNOB_A": ["", "1"], "KNOB_B": ["x", "y"]}


def spec(**over):
    base = {"genome": GENOME,
            "bench": 'echo "VERDICT: OK"; echo "throughput 100.0"',
            "score_regex": r"throughput\s+([0-9.eE+]+)",
            "verdict_regex": r"VERDICT:\s*(\S+)"}
    base.update(over)
    return base


def obj(tmp_path, **over):
    return CommandObjective(spec(**over), workdir=str(tmp_path))


def test_scores_a_healthy_run(tmp_path):
    t = obj(tmp_path).evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "OK" and t.score == 100.0


def test_config_reaches_the_command_as_environment(tmp_path):
    o = obj(tmp_path, bench='echo "VERDICT: OK"; echo "throughput ${KNOB_A:-7}"')
    assert o.evaluate(0, {"KNOB_A": "42", "KNOB_B": "x"}).score == 42.0
    # An empty gene value must UNSET the variable, not set it to "" - that is
    # what makes element 0 of a gene a true stock build.
    assert o.evaluate(1, {"KNOB_A": "", "KNOB_B": "x"}).score == 7.0


def test_env_template_is_available_to_the_command(tmp_path):
    o = obj(tmp_path,
            bench='echo "VERDICT: OK"; echo "throughput 1"; echo "SEEN {env}"')
    t = o.evaluate(0, {"KNOB_A": "1", "KNOB_B": "y"})
    assert t.status == "OK"


def test_build_failure_is_invalid(tmp_path):
    t = obj(tmp_path, build="echo 'fatal error: nope' >&2; exit 1"
            ).evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "BUILD_FAIL" and t.score is INVALID
    assert "log=" in t.detail


def test_pipefail_catches_a_failed_build_behind_a_pipe(tmp_path):
    """`make | tail` reports tail's status. Without pipefail this looks OK."""
    t = obj(tmp_path, build="(echo boom >&2; exit 1) | tail -1"
            ).evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "BUILD_FAIL"


def test_bench_failure_is_invalid(tmp_path):
    t = obj(tmp_path, bench="exit 3").evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "BENCH_FAIL" and t.score is INVALID


def test_a_bad_verdict_is_refused(tmp_path):
    t = obj(tmp_path,
            bench='echo "VERDICT: MISMATCH"; echo "throughput 999999"'
            ).evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "VERDICT_FAIL" and t.score is INVALID


def test_a_missing_verdict_fails_closed(tmp_path):
    """Configured but absent is a FAILURE, not a pass."""
    t = obj(tmp_path, bench='echo "throughput 999999"'
            ).evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "VERDICT_FAIL" and t.score is INVALID


def test_an_unparseable_score_is_invalid(tmp_path):
    t = obj(tmp_path, bench='echo "VERDICT: OK"; echo "nothing here"'
            ).evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "BENCH_FAIL" and t.score is INVALID


def test_timeout_is_invalid_not_slow(tmp_path):
    t = obj(tmp_path, bench="sleep 5", bench_timeout=1
            ).evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "TIMEOUT" and t.score is INVALID


def test_plausibility_gate_refuses_a_no_op(tmp_path):
    """A kernel that does nothing runs impossibly fast. Refuse to score it."""
    o = obj(tmp_path, bench='echo "VERDICT: OK"; echo "throughput 12500"',
            plausible_max=1.6)
    o.baseline = 100.0
    t = o.evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "IMPLAUSIBLE" and t.score is INVALID


def test_plausibility_gate_allows_a_real_win(tmp_path):
    o = obj(tmp_path, bench='echo "VERDICT: OK"; echo "throughput 120"',
            plausible_max=1.6)
    o.baseline = 100.0
    assert o.evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"}).status == "OK"


def test_secondary_floor_refuses_impossible_speed(tmp_path):
    o = obj(tmp_path,
            bench='echo "VERDICT: OK"; echo "throughput 110"; echo "ms 0.2"',
            secondary_regex=r"ms\s+([0-9.]+)", plausible_min_secondary=0.6)
    o.baseline, o.baseline_secondary = 100.0, 35.5
    t = o.evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.status == "IMPLAUSIBLE"


def test_lower_is_better_is_inverted_and_raw_is_kept(tmp_path):
    o = obj(tmp_path, higher_is_better=False,
            bench='echo "VERDICT: OK"; echo "throughput 4"')
    t = o.evaluate(0, {"KNOB_A": "1", "KNOB_B": "x"})
    assert t.score == pytest.approx(0.25)
    assert "raw=4" in t.detail


def test_an_empty_genome_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        CommandObjective(spec(genome={}), workdir=str(tmp_path))
    with pytest.raises(ValueError):
        CommandObjective(spec(genome={"G": []}), workdir=str(tmp_path))


def test_spec_loads_from_file(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec()))
    o = CommandObjective.from_file(str(p), workdir=str(tmp_path))
    assert o.genome == GENOME
