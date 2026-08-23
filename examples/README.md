# Example objective specs

## `gemm_tuning.json`

A worked example of tuning a CUDA GEMM kernel's compile-time knobs. It is a
TEMPLATE, not a runnable target — it assumes a `./kernel` directory with a
Makefile that reads the `GEMM_*` variables from the environment, and a `bench`
binary that prints a correctness verdict and a throughput number.

Read it as documentation of the spec format:

* **Element 0 of every gene is the stock setting.** `""` means *leave the
  variable unset*, so the kernel's own default applies. QSRI measures element 0
  of every gene once, as the honest baseline everything else is compared to.
  Genes whose only legal value is fixed by correctness (a mandated tile shape,
  a consensus-critical constant) do not belong in the genome at all — including
  them only generates guaranteed-invalid builds.

* **`verdict_regex` runs before `score_regex`.** A fast kernel that fails its
  verdict is not a faster kernel, it is a broken one. If the regex is configured
  and does not match, the trial is `VERDICT_FAIL` — a check that cannot say "no"
  cannot be trusted when it says nothing.

* **`plausible_max` bounds the result physically.** A verdict that only checks
  self-consistency — "the fused time equals the sum of the stage times" — is not
  a correctness check: a kernel that silently does nothing passes it and reports
  an absurd speedup. Set the bound from what the hardware can actually do.

* **`lock_file` serialises builds that share one tree.** Two concurrent builds
  in one tree run each other's `clean` and delete each other's objects, giving a
  make that exits 0 having built nothing.

Point QSRI at it with:

```bash
python -m qsri --spec examples/gemm_tuning.json --generations 40 --budget-hours 2
```
