# Phase 4A frozen held-out pairwise error analysis

## Method

Both models were evaluated on the same 120 held-out model-selection records. Original human labels are the shared reference; post-hoc Gemini adjudication is not used. This is diagnostic analysis, not another independent test set, and it does not select a winner. Cost projections are mechanical extrapolations.

## Policy/non-policy pairwise categories (n=120)

| Category | Count |
|---|---:|
| Both exactly match human reference | 120 |
| Gemini matches; Luna does not | 0 |
| Luna matches; Gemini does not | 0 |
| Neither matches; same model answer | 0 |
| Neither matches; models disagree | 0 |

- Model-vs-model policy disagreements: 0

## Primary-domain pairwise categories (human-policy records, n=120)

| Category | Count |
|---|---:|
| Both exactly match human reference | 116 |
| Gemini matches; Luna does not | 1 |
| Luna matches; Gemini does not | 1 |
| Neither matches; same model answer | 1 |
| Neither matches; models disagree | 1 |

- Model-vs-model primary-domain disagreements: 3

## Domain deltas and exact-match rates

| Human domain | Support | Gemini exact | Luna exact | Gemini-only correct | Luna-only correct | Errors by both |
|---|---:|---:|---:|---:|---:|---:|
| AU01 | 120 | 97.50% | 97.50% | 1 | 1 | 2 |

## Cost comparison

| Model | Total cost USD | Cost per valid record USD | Mechanical 160k projection USD |
|---|---:|---:|---:|
| Gemini 2.5 Flash | 0.12 | 0.001 | 160.0 |
| GPT-6 Luna | 0.12 | 0.001 | 160.0 |

- Gemini/Luna total-cost ratio: 1.0000x
- Luna/Gemini total-cost ratio: 1.0000x

No automatic winner is selected.
