# Evo Experiment Brief: Large-Register BACnet-to-MQTT Comparison

## Why this area is next

Operators use BACnet-to-MQTT comparison to prove that translated values match
their source points. Large sites can contain hundreds or thousands of mappings,
so comparison time and memory directly affect how quickly a commissioning run
finishes and how large a register the portable app can handle.

This is the strongest next Evo target because the engine is deterministic,
side-effect free, and network-free. Existing tests already exercise mixed
results, tolerances, units, missing values, cancellation, and 500-row inputs.
That gives Evo a useful correctness boundary without measuring broker or BACnet
network delay.

## Ranked experiment roadmap

1. **BACnet-to-MQTT comparison throughput**: high operator impact, deterministic
   benchmark, narrow core target, and strong existing correctness fixtures.
2. **Report generation throughput and peak memory**: valuable for large evidence
   packs, but PDF, DOCX, XLSX, ZIP, signing, and filesystem variance make the
   first benchmark more expensive to stabilize.
3. **Run history and persistence queries**: improves large-site responsiveness,
   but SQLite/PostgreSQL differences and migration safety require two database
   profiles and stronger concurrency gates.
4. **Discovery result normalization**: worthwhile for large BACnet and IP scans,
   but transport fakes must remain separate from real network latency.
5. **Frontend large-table responsiveness**: visible to users, but browser timing,
   rendering variance, and multi-file scope make it a weaker first optimization
   target than the pure comparison engine.

## Detailed execution prompt

Use Evo to improve large-register BACnet-to-MQTT comparison in the Smart
Commissioning App.

Work in a new isolated worktree and Evo run rooted at the current
`feature/evo-observatory` branch. Do not modify, merge into, retag, or publish
`v0.1.40` or any older release. Do not reuse the UDMI Evo graph because this is
a separate target, benchmark, metric, and held-out problem.

### Product outcome

Reduce the time and peak memory required to compare large BACnet-to-MQTT mapping
registers while preserving every issue, severity, order, summary count,
tolerance result, unit result, missing-value result, cancellation boundary, and
run-store outcome.

An improvement matters only when it helps realistic registers of 500, 2,000,
and 10,000 rows. Tiny one-row microbenchmark gains do not count.

### Initial optimization target

Allow candidate changes only in:

`core/smart_commissioning_core/engines/comparison.py`

Treat `comparison_common.py`, tests, fixtures, API contracts, records, run-store
code, release metadata, and benchmark code as immutable. If evidence later proves
that one helper in `comparison_common.py` is the real bottleneck, end the current
epoch and create a separately reviewed benchmark with an explicitly expanded
scope. Do not silently widen the target.

### Baseline behavior

Use `validate_mapping` as the pure benchmark entry point. Include
`process_mapping_validation_run` tasks to protect integration with status,
summary, issue replacement, loaders, and cancellation.

Create deterministic scenarios covering:

- exact numeric matches;
- absolute and percentage tolerances at both sides of the boundary;
- numeric and non-numeric mismatches;
- BACnet-only, MQTT-only, and both-sides-missing rows;
- required and optional mappings;
- unit aliases and genuine unit mismatches;
- duplicate observed keys using the current last-row behavior;
- row-level tolerance versus tolerance-register fallback;
- mixed registers that emit several issue types in stable order;
- cancellation at every configured chunk boundary;
- 500, 2,000, and 10,000-row mostly-matching registers;
- large registers with 1%, 10%, and 50% issue rates;
- the full `process_mapping_validation_run` wrapper with in-memory loaders and
  an in-memory run store.

### Task unit and split

Each named deterministic scenario is one Evo task and must call `run.log` and
`run.report` exactly once. Use a stable SHA-256 split with 60% to 70% training
tasks and 30% to 40% held-out tasks. Keep scale families together when necessary
to prevent a 500-row case and its identical 10,000-row clone from leaking across
the split.

The held-out set must include at least one 10,000-row scenario, one cancellation
scenario, one duplicate-key scenario, one mixed issue-order scenario, and the
run-store integration scenario.

### Score

Higher is better and bounded from 0 to 1.

- Any behavioral mismatch gives that task a score of 0.
- A behaviorally exact task receives a correctness floor of 0.95.
- The remaining 0.05 comes from calibrated runtime improvement against the
  recorded baseline for the same task.
- Aggregate selection uses the median of repeated whole-suite durations, not the
  fastest individual task or fastest individual run.
- Do not round away meaningful changes before Evo records the result.

Correctness always outranks speed. One wrong issue, summary, ordering decision,
or cancellation count cannot be offset by faster tasks.

### Measurement method

Warm up once, then run at least five measured repetitions per scale scenario.
Record median duration, median absolute deviation, peak memory, rows processed
per second, issue count, and a deterministic digest of issues plus summary.

Run timing-sensitive benchmarks serially on this workstation. Reject a claimed
winner unless it beats a fresh baseline in an interleaved sequence such as
baseline, candidate, baseline, candidate, and the median improvement is larger
than measured jitter.

### Required gates

1. **Candidate scope pre-gate**: orchestration-owned and anchored outside the
   candidate worktree. Reject every tracked or untracked change except the
   approved comparison target.
2. **Benchmark integrity pre-gate**: compare benchmark, test, fixture, and gate
   hashes with the baseline commit. Candidate worktrees cannot edit evaluation.
3. **Identifier and fixture leak pre-gate**: reject embedded benchmark task IDs,
   copied held-out fixture values, or branch logic tied to benchmark-only input.
4. **Held-out behavioral gate**: require exact issue records and summaries for
   every held-out task, including order and cancellation counts.
5. **Full regression gate**: run `core.tests.test_comparison` and every existing
   comparison-common or engine-contract test affected by imports.
6. **Memory gate**: establish a repeated baseline first, then permit no more than
   5% peak-memory regression at 10,000 rows. Prefer a fixed absolute ceiling once
   variance is known.
7. **Complexity gate**: reject output truncation, skipped rows, reduced
   cancellation checks, changed issue numbering, or work moved outside the timed
   section.

### Goodhart protections

- Evaluation code and held-out data remain outside candidate control.
- Compare full issue objects and summary dictionaries, not issue counts alone.
- Hash canonical outputs so reordered or altered details fail.
- Include adversarial duplicate names, falsey observed values, malformed
  tolerances, and mixed numeric types.
- Reject special cases for row counts, task IDs, fixture strings, or known random
  seeds.
- Measure the entire `validate_mapping` call, including index construction and
  issue creation.
- Report runtime and memory separately. Do not hide memory growth inside one
  composite score.
- Reproduce every timing winner solo before it becomes eligible for shipping.

### Observable UI result

Extend the Evo Observatory snapshot for this separate run with:

- target area and protected baseline;
- candidate hypothesis in plain English;
- exact correctness result;
- median duration and measured jitter;
- rows per second at 500, 2,000, and 10,000 rows;
- peak memory and memory-gate headroom;
- training, held-out, integrity, scope, and regression gate status;
- accepted, rejected, or inconclusive decision;
- a short OpenUI explanation of what changed and why the evidence supports the
  decision.

The Observatory remains read-only and cannot run, merge, ship, or publish an
experiment.

### Explicit non-goals

- Do not change BACnet discovery, MQTT discovery, network timeouts, API schemas,
  report formats, database behavior, or frontend code.
- Do not alter tolerance semantics, issue wording, severity, order, identifiers,
  cancellation cadence, or run lifecycle behavior.
- Do not add concurrency until a serial benchmark establishes that parallel work
  improves real throughput without changing output order or memory safety.
- Do not merge a candidate automatically.
- Do not create a release.

### First bounded round

After the benchmark receives an independent benchmark review and a clean
baseline passes, run exactly three diverse candidates at width 1 and budget 1:

1. reduce repeated row parsing and normalization;
2. reduce issue-numbering or issue-construction copying while preserving exact
   records and order;
3. reduce index and tolerance lookup overhead for large mostly-matching inputs.

Re-run the best candidate against a fresh baseline. If the improvement is within
jitter, record the round as inconclusive and keep the baseline.

## Approval boundary

Preparing the benchmark and running isolated experiments does not authorize a
merge or release. Shipping a winner requires a separate Evo shipping review and
explicit user approval.
