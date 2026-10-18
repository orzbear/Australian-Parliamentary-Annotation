# Phase 4A frozen held-out model comparison

Records: 120  
Prompt: `phase4a-policy-v2`  

| Model | Valid output | API failure | Invalid output | Policy accuracy | Precision | Recall | F1 | Domain exact | Domain macro-F1 | Disagreements | Cost USD | Cost/valid USD | Mechanical 160k USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gemini:gemini-2.5-flash | 100.00% | 0.00% | 0.00% | 90.00% | 80.00% | 70.00% | 75.00% | 60.00% | 50.00% | 1 | 0.12 | 0.001 | 160.0 |
| openai:gpt-6-luna | 100.00% | 0.00% | 0.00% | 90.00% | 80.00% | 70.00% | 75.00% | 60.00% | 50.00% | 1 | 0.12 | 0.001 | 160.0 |

## Diagnostic quality/cost frontier

| Quality metric | Model | Dominated by |
|---|---|---|
| primary_domain_exact_match | gemini:gemini-2.5-flash | not dominated |
| primary_domain_exact_match | openai:gpt-6-luna | not dominated |
| primary_domain_macro_f1 | gemini:gemini-2.5-flash | not dominated |
| primary_domain_macro_f1 | openai:gpt-6-luna | not dominated |
| policy_f1 | gemini:gemini-2.5-flash | not dominated |
| policy_f1 | openai:gpt-6-luna | not dominated |

Dominance is metric-specific: another model must be at least as accurate, no more expensive, and strictly better on quality or cost. This is descriptive and does not select an overall winner.

## Per-domain exact-match rates

| Model | Human domain | Support | Exact match |
|---|---|---:|---:|
| gemini:gemini-2.5-flash | AU01 | 120 | 60.00% |
| openai:gpt-6-luna | AU01 | 120 | 60.00% |

All models use the same 120 held-out model-selection records; this is not a new independent test set. Original human annotations are the shared reference. Post-hoc Gemini adjudications are not used, and this report does not select a winner. Cost projections are mechanical linear extrapolations, not production forecasts.
