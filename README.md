# QSRI — Quantum Self-Recursive Improvement

**Brion Quantum AI Team**

A self-improving search that tunes a target by measuring it, and tunes its own
search by measuring itself.

QSRI carries a **complex amplitude** per configuration value and selects by the
Born rule, so settings that win *together* rotate into alignment and reinforce;
settings that lose together cancel. It draws those selections by measuring a
circuit that **encodes the learned distribution** — on a local simulator or on
real QPU hardware. And it closes the recursion: the engine measures its own
regret and retunes its own exploration parameters, improving the thing that does
the improving.

Everything is **fail-closed**. A failed build, a failed benchmark, a missing
verdict or a timeout yields `INVALID` — never a number, never a ranking.

---

## Quick start

```bash
git clone https://github.com/Brionengine/Quantum-Self-Recursive-Improvement
cd Quantum-Self-Recursive-Improvement

# no hardware, no dependencies: run the synthetic landscape
python -m qsri --demo --generations 60

# point it at a real target
python -m qsri --spec examples/gemm_tuning.json --generations 40 --budget-hours 2

# and at a real QPU
python -m qsri --spec examples/gemm_tuning.json --qpu WK_C180
```

Pure standard library at the core. `qiskit` + `qiskit-aer` enable simulator
sampling; `pyqpanda3` enables the hardware path. Without either, QSRI falls back
to classical weighted sampling and still runs.

---

## The three algorithms

### QAIS — Quantum Amplitude Interference Search

`state[gene][value]` is a **complex amplitude**; P(value) = |amp|² normalised
across the gene. Feedback rotates phase as well as scaling magnitude — every
gene value in one configuration rotates by the *same* angle, so a winning
combination interferes constructively next draw and a losing one cancels.

This is the point of using amplitudes at all. A scalar weight per value cannot
represent *"these two settings are only good in each other's company"*, and in
real tuning spaces the knobs are strongly coupled. The test suite makes this
concrete: `SyntheticObjective` hides an optimum where one setting only pays off
in the presence of another, and asserts the engine ends up believing in both.

Temperature reshapes the Born distribution — T → ∞ is uniform, T → 0 is argmax —
so exploration and exploitation are one dial, not two code paths.

### QMRA — Quantum Meta-Recursive Ascent

The recursion proper. QMRA watches what fraction of recent trials came in above
average and tunes QSRI's **own** temperature and interference strength: thrashing
raises temperature and softens commitment, a productive slope cools and sharpens.

Two details that are easy to get wrong, and were:

* The success signal is *"was this draw above average"*, **not** *"did it beat
  the all-time best"*. The latter becomes impossible near the optimum, driving
  regret to 1.0 and pinning temperature at its cap — the engine would explore
  randomly forever and never exploit.
* A flaky toolchain must **not** count as regret. Letting build failures feed
  QMRA pinned temperature at max under a 15% failure rate: permanent random
  search dressed up as adaptation.

### Amplitude-encoded selection

The usual "quantum-assisted search" builds *n* Hadamards, runs some shots, and
takes the most frequent outcome modulo the number of options. Under uniform
superposition every outcome is equally likely — that argmax is **sampling
noise** — and the learned weights never enter the circuit at all. Such a path
discards everything the search has learned and performs strictly worse than its
own classical fallback. QGACE, this project's own ancestor, did exactly that.

QSRI loads the learned distribution into the state with RY rotations along the
standard amplitude-encoding tree, so **P(measure i) = pᵢ**. Measurement becomes a
genuine draw from what was learned, and *one shot suffices* — which is also what
makes real hardware affordable.

Two failure modes this design has to survive, both covered by tests:

* **Never argmax the shots.** Each shot is already an independent draw; taking
  the most frequent of a handful is a mode estimate, not a draw.
* **Endianness.** Qiskit reports little-endian while the encoding tree splits on
  qubit 0 first. Get it wrong and the *middle* outcomes silently transpose — a
  4-way draw measured .488/.159/.253/.100 against a target of .50/.25/.15/.10.
  The endpoints look perfect, which is exactly the kind of error that survives a
  careless eyeball check.

**On real hardware** (`--qpu`), the whole genome is drawn in **one** circuit. The
genes are independent, so their joint distribution is a product state: gene *i*
gets ⌈log₂(values)⌉ qubits and a single measurement of the full register yields a
complete configuration. A job costs seconds of queue; one job per gene would make
the hardware path slower than the search it guides — decorative quantum, which is
worse than none. The nine-gene example needs 15 qubits; WK_C180 has 180.

---

## Fail-closed evaluation

| Status | Meaning | Learned from |
|---|---|---|
| `OK` | verdict passed, score parsed, within plausible bounds | yes |
| `VERDICT_FAIL` | correctness check failed **or did not appear** | hard penalty |
| `IMPLAUSIBLE` | ran, but produced a physically impossible result | hard penalty |
| `BUILD_FAIL` / `BENCH_FAIL` / `TIMEOUT` | toolchain trouble | soft penalty only |

Three rules the engine will not bend:

1. **Correctness before speed.** A fast result that fails its verdict is not a
   faster result, it is a broken one. A verdict that is configured but does not
   appear in the output is a *failure*: a check that cannot say "no" cannot be
   trusted when it says nothing.
2. **Bound the result physically.** A verdict that only checks self-consistency —
   *"the fused time equals the sum of the stage times"* — is not a correctness
   check. A target that silently does **nothing** passes it and reports an absurd
   speedup; trusting one scored a no-op at 125× baseline. `plausible_max` refuses
   that outright instead of letting the search spend its budget chasing it.
3. **Distinguish the configuration's fault from the toolchain's.** A correctness
   failure is reproducible information about the landscape and earns a hard
   penalty. A random build failure is not, and hammering the genes for it erases
   real knowledge — measured collapsing belief from 6/7 genes to 2/7.

The ledger is written **atomically** after every trial (`os.replace`), so a
killed run cannot leave a truncated file, and history is replayed into the
amplitudes on restart. Improvement survives the process that produced it.

---

## Pointing QSRI at your own target

Write a JSON spec. The engine never changes.

```json
{
  "genome": {
    "MY_TILE":   ["", "64", "128"],
    "MY_POLICY": ["default", "aggressive"]
  },
  "build": "{env} make -j8 2>&1 | tail -25",
  "bench": "./bench --iters 8",
  "verdict_regex": "VERDICT:\\s*(\\S+)",
  "score_regex": "throughput\\s+([0-9.eE+]+)",
  "plausible_max": 1.6
}
```

* **Element 0 of every gene is the stock setting**, and `""` means *leave the
  variable unset* so the target's own default applies. QSRI measures the stock
  configuration once as the honest baseline everything is compared against.
* Configuration reaches your commands as **environment variables**, and as
  `{env}` in the command template for make-style invocation.
* Knobs fixed by correctness — a mandated tile shape, a protocol constant — do
  **not** belong in the genome. Including them only generates guaranteed-invalid
  builds.
* On a shared build tree set `lock_file`. Two concurrent builds in one tree run
  each other's `clean` and delete each other's objects, producing a make that
  exits 0 having built nothing.

See [`examples/`](examples/) for the full annotated spec, and
[`qsri/objectives.py`](qsri/objectives.py) for every key.

For anything a shell cannot express, implement `Objective` directly: supply
`genome`, `units` and `evaluate(gen, config) -> Trial`. That is the entire
interface.

---

## Layout

```
qsri/
├── engine.py       QAIS, QMRA, the six-phase loop, the atomic ledger
├── samplers.py     amplitude encoding — Aer simulator and QPU hardware
├── objectives.py   Objective protocol, CommandObjective, SyntheticObjective
└── cli.py          python -m qsri

quantum_auto_scaler/   QDSS — quantum demand superposition scaling (see below)
examples/              annotated objective specs
tests/                 engine, objective and encoding tests
```

---

## Lineage

QSRI is an update of Brion's own earlier self-recursive improvement work. Two
repositories are the direct ancestors:

* **QGACE** — Quantum-Guided Autonomous Code Evolution
  ([`Brion-Quantum-A.I.-General-System`](https://github.com/Brionengine/Brion-Quantum-A.I.-General-System)).
  Contributes the improvement cycle — analyze, benchmark, transform, test,
  accept or roll back — with weights that evolve on historical success, atomic
  rollback and convergence detection. QSRI replaces its quantum selection step,
  which was measuring noise.
* **QDSS** — Quantum Demand Superposition Scaling, the `quantum_auto_scaler`
  package that ships alongside QSRI here. Contributes modelling a decision space
  as a superposition and collapsing it by measurement when a choice must be made.

QSRI is **not** descended from RecursiveSelfRegeneration — that is self-recursive
self-*healing*, regenerating degraded state, which is a different algorithm.

---

## Also in this repository: QDSS

`quantum_auto_scaler/` is the auto-scaling system QSRI inherits its
superposition-and-collapse idea from, kept here as the working ancestor.

```
quantum_auto_scaler/
├── core/         auto_scaler (QDSS), demand_predictor, resource_optimizer, health_monitor
├── quantum/      Cirq QAOA optimizer, TensorFlow predictor, TFQ hybrid, backend manager
├── scaling/      reactive / predictive / quantum policies, TPU scaler, cost optimizer
└── utils/        metrics
```

Novel algorithms: **QDSS** (future demand as a superposition), **QAOA resource
allocation**, and a **Quantum Boltzmann predictor** (forecasting by thermal
sampling).

---

## Tests

```bash
pip install pytest        # qiskit + qiskit-aer to include the encoding tests
python -m pytest tests/ -q
```

The suite is written to catch the mistakes this design is *about*: an objective
that lies, a toolchain that flakes, an encoding that transposes, a sampler that
collapses to argmax, and a ledger that must survive a restart.

---

## Optional dependencies

The repository imports without the heavy scientific stack (numpy, torch,
tensorflow, qiskit, cirq, pyqpanda3). Clone it and run it; install only what the
parts you actually use need. See [OPTIONAL_DEPENDENCIES.md](OPTIONAL_DEPENDENCIES.md).

---

*Developed by the Brion Quantum AI Team.*
