# Memory write-gate promotion evaluation

Offline, read-only evaluation for RFC #1770 and issue #1930. It never changes Memory revisions, policy modes or the
private observation ledger. The existing Runtime owns the gate, its question and outcome mapping.

## Prepare the fixed public dataset

[PROTOCOL.md](PROTOCOL.md) defines sampling, label mapping and calibration selection. Download the official
[XNLI 1.0 archive](https://dl.fbaipublicfiles.com/XNLI/XNLI-1.0.zip) outside Git. Its pinned SHA-256 is
`4ba1d5e1afdb7161f0f23c66dc787802ccfa8a25a3ddd3b165a35e50df346ab1`.

Choose your input and output locations before running the commands. Set `MEMORY_GATE_ARCHIVE` to the downloaded
archive, `MEMORY_GATE_DATA_DIR` to a new generated-input directory, and `MEMORY_GATE_OUTPUT_DIR` to an output directory.
Keep generated inputs and outputs outside version control. For cloud calls, set `MEMORY_GATE_KEY_FILE` to your credential
file, or use the supported API-key environment variable.

From a clean checkout, generate 600 calibration and 1,800 held-out reference-only cases:

```bash
uv sync --locked --group dev
mkdir -p "$MEMORY_GATE_OUTPUT_DIR"
uv run python -m evaluation.memory.write_gate.prepare_public \
  --archive "$MEMORY_GATE_ARCHIVE" --output-dir "$MEMORY_GATE_DATA_DIR"
uv run python -m evaluation.memory.write_gate \
  --calibration "$MEMORY_GATE_DATA_DIR/calibration.json" \
  --held-out "$MEMORY_GATE_DATA_DIR/held_out.json" \
  --public-archive "$MEMORY_GATE_ARCHIVE" --backend no_call \
  --output "$MEMORY_GATE_OUTPUT_DIR/input-coverage.json"
```

Preparation refuses an existing output directory. The generated manifest hashes must match
[data/xnli/preparation.json](data/xnli/preparation.json). Git contains the preparation rules and small summaries;
expanded manifests, the raw corpus and new run output stay outside version control. XNLI has a CC BY-NC 4.0 license.
Labels never enter model requests. Translation groups and duplicate text do not cross splits.

`no_call` resolves inputs without model inference. Its `input_coverage_audit` report preserves local preflight HOLD,
with no selected model configuration. Local-rule judgments are not model-quality evidence.

## Live Jev evaluation

The public backend calls `typesafe/jev-1.13` through OpenRouter's Decisions API using the existing System One adapter.
It accepts only the pinned public archive, never private Server references or arbitrary text. It captures the gate's
question, makes the cloud request outside Runtime, and rescores the stored answer locally. The cloud model is not
marked local. This does not implement Runtime hosted-redacted transport or qualify a hosted deployment.

Supply an OpenRouter key from a file outside the repository, `POWERCONTEXT_EVAL_JEV_API_KEY` or `OPENROUTER_API_KEY`.
An ordinary OpenAI credential is not reused. Keys and raw error responses are not written to reports.

Run calibration first:

```bash
uv run python -m evaluation.memory.write_gate \
  --backend jev --calibration-only \
  --calibration "$MEMORY_GATE_DATA_DIR/calibration.json" \
  --held-out "$MEMORY_GATE_DATA_DIR/held_out.json" \
  --public-archive "$MEMORY_GATE_ARCHIVE" --jev-key-file "$MEMORY_GATE_KEY_FILE" \
  --output "$MEMORY_GATE_OUTPUT_DIR/jev-calibration.json"
```

Compare the eight calibration configurations, freeze a selection, then use the following held-out command.
The example configuration must be replaced if calibration selects another:

```bash
uv run python -m evaluation.memory.write_gate \
  --backend jev --configuration 'hold_on=yes;threshold=none' \
  --calibration-report "$MEMORY_GATE_OUTPUT_DIR/jev-calibration.json" \
  --calibration "$MEMORY_GATE_DATA_DIR/calibration.json" \
  --held-out "$MEMORY_GATE_DATA_DIR/held_out.json" \
  --public-archive "$MEMORY_GATE_ARCHIVE" --jev-key-file "$MEMORY_GATE_KEY_FILE" \
  --output "$MEMORY_GATE_OUTPUT_DIR/jev-held-out.json"
```

Each input makes at most one request, without automatic retry; all configurations reuse the answer. Calibration reuse
requires matching data, model, question and code. A partial or mismatched report cannot serve as a current cache.
Without `--calibration-report`, the held-out command also reruns calibration. A sibling `.cases.jsonl` checkpoint
flushes each bounded result. Credit, authentication or rate-limit refusal stops new calls; skipped cases are
`not_attempted`. Interruptions can leave checkpoints without an aggregate report. Output paths must be new.

## Report identity

New reports expose a path-to-SHA-256 code manifest including the shared assessor, question, Memory gate protocols and
inference result/error contracts. Its canonical digest invalidates old calibration after any covered code change.
Historical answers stay historical evidence and are not accepted as current-code calibration caches.

## Authorized private inputs

Private manifests retain references and labels, not production text. `resolver.py` uses current Scope authorization
and exact Artifact revisions through `PowerContextClient`. Provide `--server-url` and `POWERCONTEXT_CLIENT_API_TOKEN`.
Source reads require a pinned `gate_input_digest` matching the reconstructed original projection. Pending
`candidate:1` references cannot be recovered as immutable Memory. Missing, unauthorized or unsupported input is
reported separately from a negative judgment. These limits are not silently replaced with current content.

Use `--backend no_call --server-url "$MEMORY_GATE_SERVER_URL"` with private manifests to audit input coverage.
For authorized re-evaluation with an existing local-only `DecisionModel`, use the Python interface:

```python
from evaluation.memory.write_gate.dataset import load_dataset
from evaluation.memory.write_gate.resolver import AuthorizedClientResolver, SplitCheckedResolver


def prepare_private_evaluation(calibration_path, held_out_path, client):
    calibration, _ = load_dataset(calibration_path)
    held_out, _ = load_dataset(held_out_path)
    resolver = SplitCheckedResolver(calibration, held_out, AuthorizedClientResolver(client))
    return calibration, held_out, resolver
```

The caller supplies a configured `PowerContextClient` and local-only model. The Runtime privacy boundary still applies.
Share this resolver between both splits: it compares each authorized candidate/evidence projection before scoring,
retaining only hashes and performing no additional reads. Missing, unauthorized or unsupported inputs remain separate
coverage results.
Use `evaluate_dataset(dataset, resolver, model=local_model)` from `runner` to run calibration before choosing a
configuration, then evaluate held-out and pass both result sets to `report(..., selected_configuration=...)`.
Input labels are used only for scoring. Controlled models in tests verify program behavior; they do not measure
model quality.

## Read the measurements

`input_coverage` counts resolved cases; `adjudication_coverage` counts substantive local/model judgments.
Unresolved cases do not enter error denominators. Abstention and fallback are unadjudicated; fail-open cases count
in the separate end-to-end false-pass effect. `used_fallback=true` always implies `coverage=unadjudicated`.
FLAG permits writing, so it counts as a pass. Missing confidence does not downgrade HOLD under current Runtime rules.

Latency and cost describe the recorded attempt; retries require accounting for earlier attempts separately.
Backend attempts and provider-returned request counts differ. Missing usage leaves full token/currency totals unknown,
with a separately named known subtotal. HOLD/FLAG rates describe this sample; human review time and production
friction are unmeasured. Jev confidence is not a calibrated probability of correctness; the provider ID does not
attest immutable weights. XNLI does not establish production, multi-citation or training-overlap performance.
See [the measured results](results/jev-1.13/RESULTS.md) for the measured recommendation and rollback procedure.
