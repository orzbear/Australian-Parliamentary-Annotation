# Phase 4A held-out post-hoc adjudication analysis

**This is post-hoc adjudication/error analysis, not the original held-out benchmark.**

## Original human-reference held-out result

These values are repeated from the frozen original `metrics.json`; they are not replaced or recalculated here.

- Selected records: 4
- Valid predictions: 4
- Policy/non-policy accuracy: 50.00%
- Policy precision: 50.00%
- Policy recall: 50.00%
- Policy F1: 50.00%
- Primary-domain exact match: 50.00%
- Primary-domain macro-F1: 40.00%
- Original disagreements: 2

## Adjudication outcomes

- Total adjudicated mismatches: 2
- Original agreements not manually disputed: 2
- Human correct: 0
- Human needs revision: 1
- Model error: 0
- Codebook ambiguous: 1
- Human-label revision recommendations: 1
- Unresolved taxonomy/codebook cases: 1

## Adjudicated policy-gate analysis

- Accuracy: 100.00%
- Precision: 100.00%
- Recall: 100.00%
- F1: 100.00%
- Policy labels changed by adjudication: 1

## Adjudicated primary-domain analysis

- Resolvable policy-domain cases: 1
- Excluded unresolved taxonomy ambiguity: 1
- Exact match: 100.00%
- Macro-F1: 33.33%

| Domain | Support | F1 |
|---|---:|---:|
| AU01 | 0 | 0.00% |
| AU02 | 0 | 0.00% |
| AU03 | 1 | 100.00% |

## Error-analysis summary

| Issue type | Count | General adjudication tendency |
|---|---:|---|
| domain_boundary | 1 | remained ambiguous |
| policy_gate | 1 | favoured model prediction |

## Methodological warning

The 120-record set remains the held-out validation/model-selection set. Adjudication occurred after observing model disagreements, so these adjusted metrics are diagnostic and post-hoc and do not replace the original human-reference benchmark. phase4a-policy-v2 remains frozen; future substantive prompt changes require V3 and a new evaluation strategy.
