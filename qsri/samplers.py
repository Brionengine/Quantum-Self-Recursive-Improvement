"""
Circuit-backed samplers.

QSRI's selection step draws a value from a LEARNED distribution by measuring a
circuit whose amplitudes encode that distribution. Two backends implement it:

  * :class:`QuantumSampler` - one gene per circuit on a local Aer simulator.
  * :class:`OriginSampler`  - the whole genome as one product state on real
    Origin Quantum QPU hardware.

Both are fail-closed: any transport, queue, encoding or decode problem returns
None and the caller falls back to the next backend, then to classical sampling.
A sampler is never allowed to fabricate a configuration.
"""
from __future__ import annotations

import math
import os
import random
from typing import Dict, List, Optional, Tuple


def _tree_angles(probs: List[float], k: int) -> List[Tuple[int, int, float]]:
    """
    (level, node_index, theta) for the amplitude-encoding tree over k qubits.

    theta_node = 2*arccos(sqrt(P(left subtree) / P(node))). This is the standard
    construction: applying these rotations to |0...0> yields a state whose
    measurement probabilities are exactly ``probs``.
    """
    n = 1 << k
    p = list(probs) + [0.0] * (n - len(probs))
    angles: List[Tuple[int, int, float]] = []
    level_masses = [p]                       # mass under each node, per level
    while len(level_masses[0]) > 1:
        cur = level_masses[0]
        level_masses.insert(0, [cur[i] + cur[i + 1] for i in range(0, len(cur), 2)])
    for level in range(k):
        masses = level_masses[level]         # 2**level nodes
        child = level_masses[level + 1]
        for node in range(len(masses)):
            tot = masses[node]
            if tot <= 0:
                angles.append((level, node, 0.0))
                continue
            ratio = min(max(child[2 * node] / tot, 0.0), 1.0)
            angles.append((level, node, 2.0 * math.acos(math.sqrt(ratio))))
    return angles


def _weighted_pick(counts: Dict[str, int]) -> str:
    """Sample one bitstring in proportion to its count.

    Deliberately NOT argmax. Each shot is already an independent draw from the
    encoded distribution; taking the most frequent of a handful of shots is a
    mode estimate, not a draw, and collapsing to it destroys the distribution
    the encoding exists to represent.
    """
    total = sum(counts.values()) or 1
    r = random.random() * total
    acc = 0.0
    for bits, c in counts.items():
        acc += c
        if r <= acc:
            return bits
    return next(iter(counts))


class QuantumSampler:
    """
    Draw one gene value by measuring a circuit that encodes the learned
    distribution over that gene's values.

    Why encoding matters. The common formulation of "quantum-assisted search"
    builds n Hadamards, runs some shots, and takes the most frequent outcome
    modulo the number of options. Under uniform superposition every outcome is
    equally likely, so that argmax is sampling noise - and the learned weights
    never enter the circuit at all. Such a path throws away everything the
    search has learned and performs strictly worse than its own classical
    weighted-random fallback.

    Here the target distribution is loaded into the state with RY rotations, so
    P(measure basis state i) == p_i. Measurement is then a genuine sample from
    the learned distribution rather than from noise, and ONE shot suffices -
    which is also what makes running it on real QPU hardware affordable.
    """

    def __init__(self, backend: str = "auto", shots: int = 1):
        self.shots = shots
        self.backend_name = "classical"
        self._qk = None
        self._aer = None
        self.draws = 0
        self.quantum_draws = 0
        if backend in ("auto", "qiskit"):
            try:
                from qiskit import QuantumCircuit, transpile
                from qiskit_aer import Aer
                self._qk = (QuantumCircuit, transpile)
                self._aer = Aer
                self.backend_name = "qiskit-aer"
            except ImportError:
                self._qk = None

    def draw(self, dist: List[Tuple[str, float]]) -> Optional[str]:
        """Return a value sampled by circuit measurement, or None to fall back."""
        self.draws += 1
        if self._qk is None or len(dist) < 2:
            return None
        values = [v for v, _ in dist]
        probs = [p for _, p in dist]
        k = max(1, (len(values) - 1).bit_length())
        try:
            QuantumCircuit, transpile = self._qk
            qc = QuantumCircuit(k)
            for level, node, theta in _tree_angles(probs, k):
                if abs(theta) < 1e-12:
                    continue
                if level == 0:
                    qc.ry(theta, 0)
                    continue
                # Controlled on the bit-pattern of `node` across qubits 0..level-1
                ctrl = list(range(level))
                pattern = format(node, "0%db" % level)
                flips = [ctrl[i] for i, b in enumerate(pattern) if b == "0"]
                for q in flips:
                    qc.x(q)
                if hasattr(qc, "mcry"):
                    qc.mcry(theta, ctrl, level)
                else:
                    qc.ry(theta, level)
                for q in flips:
                    qc.x(q)
            qc.measure_all()
            sim = self._aer.get_backend("qasm_simulator")
            res = sim.run(transpile(qc, sim), shots=self.shots).result()
            counts = res.get_counts()
            bits = _weighted_pick(counts)
            # Qiskit reports little-endian: qubit 0 is the RIGHTMOST character.
            # The encoding tree splits on qubit 0 first, i.e. treats it as most
            # significant, so the string must be reversed before int(). Without
            # this the middle outcomes silently transpose - a 4-way draw
            # measured a=.488 b=.159 c=.253 d=.100 against a target of
            # .50/.25/.15/.10: b and c swapped, endpoints unaffected, which is
            # exactly the kind of error that survives a careless eyeball check.
            idx = int(bits.replace(" ", "")[::-1], 2)
            if idx < len(values):
                self.quantum_draws += 1
                return values[idx]
            return None            # out-of-range basis state: fall back, do not
                                   # silently fold it back with a modulo
        except Exception:
            return None

    # kept as a public alias: older call sites used this name
    rng_pick = staticmethod(_weighted_pick)


class OriginSampler:
    """
    Draw an ENTIRE configuration in one shot on an Origin Quantum QPU.

    Why one circuit and not one per gene: a real QPU job costs seconds of queue
    plus execution. A search needs one draw per gene per generation, and one job
    per gene would make the hardware path slower than the search it guides -
    decorative quantum, which is worse than none.

    The genes are independent, so their joint distribution is a PRODUCT state.
    One circuit therefore carries all of them: gene i gets ceil(log2(values_i))
    qubits, RY-encoded to its learned distribution, and a single measurement of
    the full register yields a complete configuration. A nine-gene genome of the
    size shipped in ``examples/`` needs 15 qubits; WK_C180 has 180, so the whole
    search space fits with room to spare.

    Controlled-RY is decomposed as RY(t/2) CNOT RY(-t/2) CNOT, since the chip's
    native set is rotations plus CNOT/CZ.
    """

    #: Files searched for an API key, after the environment variables.
    KEY_PATHS = ("~/.config/qsri/origin.key",
                 "~/.config/origin/api.key")

    # Origin rejects shots outside [10, 49600] ("field 'shot' has over the
    # range"), so the one-shot ideal is not available on hardware. That is fine:
    # with amplitude encoding EVERY shot is an independent draw from the learned
    # distribution, so we take the minimum the chip allows and pick one shot
    # from the returned distribution at random.
    MIN_SHOTS = 10

    def __init__(self, backend: str = "WK_C180", shots: int = MIN_SHOTS,
                 max_qubits: int = 180):
        self.backend = backend
        self.shots = max(int(shots), self.MIN_SHOTS)
        self.max_qubits = max_qubits
        self.jobs = 0
        self.failures = 0
        self.last_error = ""
        self._svc = None
        self._core = None
        self.available = False
        try:
            from pyqpanda3 import qcloud, core
            key = self._load_key()
            if key:
                self._svc = qcloud.QCloudService(api_key=key)
                self._core = core
                self.available = True
            else:
                self.last_error = "no API key (set ORIGIN_QC_API_KEY)"
        except Exception as exc:                       # noqa: BLE001
            self.last_error = "%s: %s" % (type(exc).__name__, exc)

    @classmethod
    def _load_key(cls) -> Optional[str]:
        k = os.environ.get("ORIGIN_QC_API_KEY") or os.environ.get("ORIGINQ_API_KEY")
        if k:
            return k.strip()
        for p in cls.KEY_PATHS:
            try:
                with open(os.path.expanduser(p)) as fh:
                    txt = fh.read().strip()
            except OSError:
                continue
            if not txt:
                continue
            if "=" in txt:
                for line in txt.splitlines():
                    if "=" in line and "API_KEY" in line:
                        return line.split("=", 1)[1].strip().strip("\"'")
            else:
                return txt
        return None

    def draw_config(self, dists: Dict[str, List[Tuple[str, float]]]
                    ) -> Optional[Dict[str, str]]:
        if not self.available:
            return None
        layout, total = [], 0
        for gene, dist in dists.items():
            k = max(1, (len(dist) - 1).bit_length())
            layout.append((gene, dist, total, k))
            total += k
        if total > self.max_qubits:
            self.last_error = "needs %d qubits > %d" % (total, self.max_qubits)
            return None
        try:
            QProg, QCircuit = self._core.QProg, self._core.QCircuit
            RY, CNOT, X = self._core.RY, self._core.CNOT, self._core.X
            circ = QCircuit(total)
            for _gene, dist, base, k in layout:
                probs = [p for _, p in dist]
                for level, node, theta in _tree_angles(probs, k):
                    if abs(theta) < 1e-12:
                        continue
                    tgt = base + level
                    if level == 0:
                        circ << RY(tgt, theta)
                        continue
                    ctrls = [base + i for i in range(level)]
                    pattern = format(node, "0%db" % level)
                    flips = [ctrls[i] for i, b in enumerate(pattern) if b == "0"]
                    for q in flips:
                        circ << X(q)
                    # CRY(theta) with a single control; for level>1 use the last
                    # control (higher levels are rare in genomes of this shape).
                    c = ctrls[-1]
                    circ << RY(tgt, theta / 2) << CNOT(c, tgt) \
                         << RY(tgt, -theta / 2) << CNOT(c, tgt)
                    for q in flips:
                        circ << X(q)
            prog = QProg()
            prog << circ
            for q in range(total):
                prog << self._core.measure(q, q)
            job = self._svc.backend(self.backend).run(prog, self.shots)
            res = job.result()
            # On Origin hardware get_counts() returns EMPTY even for a FINISHED
            # job with no error_message - the distribution lives in get_probs().
            # Trusting counts alone reports "no counts" on every real run.
            counts: Dict[str, float] = {}
            try:
                probs = res.get_probs()
                if probs:
                    counts = dict(probs)
            except Exception:
                pass
            if not counts:
                try:
                    counts = dict(res.get_counts() or {})
                except Exception:
                    counts = {}
            if not counts:
                # An empty distribution is not self-explanatory: the job may
                # have FAILED in the cloud compiler. Surface the chip's own
                # status rather than reporting a bare "no counts".
                detail = ""
                try:
                    detail = "status=%s err=%s" % (res.job_status(),
                                                   res.error_message())
                except Exception:
                    pass
                self.failures += 1
                self.last_error = ("empty distribution " + detail).strip()
                return None
            self.jobs += 1
            bits = _weighted_pick(counts).replace(" ", "")
            bits = bits[::-1]                      # little-endian, as with Aer
            cfg = {}
            for gene, dist, base, k in layout:
                idx = int(bits[base:base + k][::-1] or "0", 2)
                if idx >= len(dist):
                    return None                    # invalid basis state: refuse
                cfg[gene] = dist[idx][0]
            return cfg
        except Exception as exc:                   # noqa: BLE001
            self.failures += 1
            self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            return None
