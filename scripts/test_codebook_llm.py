#!/usr/bin/env python3
"""
Test script to evaluate the draft Australian Policy Annotation Codebook against human annotations.

Evaluates:
- Non-policy classification accuracy (is_non_policy flag match)
- Primary Australian policy domain exact match (AU01-AU14, AU_OTHER_REVIEW, or null)
- Comparison metrics, confusion matrix, and detailed mismatch reporting.

Outputs:
- CLI evaluation summary table & mismatch analysis
- Raw comparison results JSON saved to data/review/llm_codebook_test_results.json
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Literal, Optional

# Attempt to load dotenv if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pydantic import BaseModel, Field, ValidationError

# Detect GenAI SDK: prefer google-genai (new official SDK), fallback to google-generativeai
HAVE_NEW_GENAI = False
HAVE_LEGACY_GENAI = False

try:
    from google import genai
    from google.genai import types as genai_types
    HAVE_NEW_GENAI = True
except ImportError:
    try:
        import google.generativeai as legacy_genai
        HAVE_LEGACY_GENAI = True
    except ImportError:
        pass


# Domain codes supported by the taxonomy
DomainCode = Literal[
    "AU01", "AU02", "AU03", "AU04", "AU05", "AU06", "AU07",
    "AU08", "AU09", "AU10", "AU11", "AU12", "AU13", "AU14",
    "AU_OTHER_REVIEW"
]


class PolicyAnnotation(BaseModel):
    """Pydantic model for structured classification output from the LLM."""
    is_non_policy: bool = Field(
        description="True if the turn is procedural, ceremonial, condolence, personal tribute, or contains no substantive public-policy discussion; False otherwise."
    )
    primary_australian_domain: Optional[DomainCode] = Field(
        default=None,
        description="The primary policy domain code (AU01 to AU14, or AU_OTHER_REVIEW) representing the main topic, or null if is_non_policy is True."
    )
    secondary_australian_domains: List[DomainCode] = Field(
        default_factory=list,
        description="Up to 2 additional substantive domain codes (AU01 to AU14, excluding AU_OTHER_REVIEW); empty if none or if is_non_policy is True."
    )
    reasoning: str = Field(
        description="Brief justification for the classification decision, explaining relevant domain boundaries and exclusion rules."
    )


def load_codebook_context(path: Path) -> Dict[str, Any]:
    """Load CODEBOOK_CONTEXT.json and validate structure."""
    if not path.is_file():
        raise FileNotFoundError(f"Codebook context file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data


def load_annotations(path: Path, max_records: int = 30) -> List[Dict[str, Any]]:
    """Load the first `max_records` from annotations.jsonl."""
    if not path.is_file():
        raise FileNotFoundError(f"Annotations file not found: {path}")

    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if idx >= max_records:
                break
            stripped = line.strip()
            if not stripped:
                continue
            record = json.loads(stripped)
            records.append(record)

    return records


def build_system_prompt(codebook_context: Dict[str, Any]) -> str:
    """
    Construct a comprehensive system prompt containing role definition,
    non-policy rules, domain definitions, and boundary guidelines.
    """
    # Extract labels from the taxonomy
    taxonomies = codebook_context.get("taxonomies", [])
    labels: List[Dict[str, Any]] = []
    for tax in taxonomies:
        if tax.get("slug") == "australian_policy_domains" or "labels" in tax:
            labels = tax.get("labels", [])
            break

    # Format domain sections
    domain_sections: List[str] = []
    for item in labels:
        code = item.get("code", "")
        label = item.get("label", "")
        short_def = item.get("short_definition", "")
        inclusion_rules = item.get("inclusion_rules", [])
        exclusion_rules = item.get("exclusion_rules", [])
        positive_examples = item.get("positive_examples", [])

        section_lines = [f"### [{code}] {label}"]
        if short_def:
            section_lines.append(f"Definition: {short_def}")

        if inclusion_rules:
            section_lines.append("Inclusion Rules:")
            for inc in inclusion_rules:
                section_lines.append(f"  - {inc}")

        if exclusion_rules:
            section_lines.append("Exclusion / Boundary Rules:")
            for exc in exclusion_rules:
                section_lines.append(f"  - {exc}")

        if positive_examples:
            section_lines.append(f"Examples: {', '.join(positive_examples)}")

        domain_sections.append("\n".join(section_lines))

    domains_text = "\n\n".join(domain_sections)

    prompt = f"""You are an expert policy classifier specializing in the Australian Federal Parliament.
Your task is to classify parliamentary speech turns according to the Australian Policy Annotation Codebook.

## CLASSIFICATION PROTOCOL

### Step 1: Procedural / Non-Policy Assessment (`is_non_policy`)
- Mark `is_non_policy: true` ONLY when the entire turn is procedural, ceremonial, or contains no substantive public-policy discussion.
  - Procedural examples: Speaker rulings, calls to order, routine tabling of petitions/reports, points of order, division announcements, motions to suspend standing orders.
  - Ceremonial examples: Personal condolences, sporting tributes, local constituent congratulations, valedictory remarks without policy advocacy.
  - If any substantive public policy argument, criticism, or government action is debated, `is_non_policy` MUST be `false`.
- If `is_non_policy` is `true`:
  - `primary_australian_domain` MUST be null.
  - `secondary_australian_domains` MUST be an empty list `[]`.

### Step 2: Primary Policy Domain (`primary_australian_domain`)
- If `is_non_policy: false`, you MUST select the single best Australian policy domain code that represents the central organising topic of the speech turn.
- Choose from: AU01 to AU14, or AU_OTHER_REVIEW.
- `AU_OTHER_REVIEW` is reserved strictly for substantive public policy that does not fit into any defined category AU01-AU14. Do NOT use it merely because coding is difficult or uncertain.

### Step 3: Secondary Policy Domains (`secondary_australian_domains`)
- Optionally select up to TWO secondary domains (from AU01 to AU14, excluding AU_OTHER_REVIEW) representing other substantive policy issues discussed.
- Do NOT code passing references or brief figures of speech.
- Do NOT duplicate the primary domain code.

### Step 4: Reasoning (`reasoning`)
- Provide a brief, concise rationale (1-3 sentences) explaining the classification and explicitly noting any boundary rules applied.

---

## AUSTRALIAN POLICY DOMAINS TAXONOMY

{domains_text}

---

## OUTPUT REQUIREMENTS
You must output strictly valid JSON conforming to this schema:
{{
  "is_non_policy": <boolean>,
  "primary_australian_domain": <string AU01-AU14, AU_OTHER_REVIEW, or null>,
  "secondary_australian_domains": [<list of strings AU01-AU14, max 2>],
  "reasoning": <string>
}}
"""
    return prompt.strip()


def build_speech_user_prompt(record: Dict[str, Any]) -> str:
    """Format speech turn metadata and speech text into user prompt."""
    speech = record.get("speech", {})
    speaker = speech.get("speaker_name", "Unknown Speaker")
    date = speech.get("date", "Unknown Date")
    chamber = speech.get("chamber", "Australian Parliament")
    major_heading = speech.get("major_heading") or ""
    minor_heading = speech.get("minor_heading") or ""
    text = speech.get("text", "")

    headings = " > ".join(filter(None, [major_heading, minor_heading]))

    user_prompt = f"""Please classify the following Australian parliamentary speech turn:

[Metadata]
Speaker: {speaker}
Chamber: {chamber}
Date: {date}
Context / Heading: {headings if headings else "N/A"}

[Speech Text]
\"\"\"
{text}
\"\"\"
"""
    return user_prompt.strip()


def clean_json_text(text: str) -> str:
    """Strip markdown code fence if present."""
    s = text.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def classify_speech_with_retry(
    client: Any,
    system_prompt: str,
    user_prompt: str,
    model_name: str = "gemini-2.5-flash",
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> PolicyAnnotation:
    """Call Gemini model with structured output, retrying on transient errors."""
    for attempt in range(1, max_retries + 1):
        try:
            if HAVE_NEW_GENAI:
                config = genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=PolicyAnnotation,
                    temperature=0.0,
                )
                response = client.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=config,
                )
                raw_text = response.text or "{}"
                cleaned = clean_json_text(raw_text)
                return PolicyAnnotation.model_validate_json(cleaned)

            elif HAVE_LEGACY_GENAI:
                # Fallback to legacy google.generativeai
                model = legacy_genai.GenerativeModel(
                    model_name=model_name,
                    system_instruction=system_prompt,
                    generation_config={
                        "response_mime_type": "application/json",
                        "response_schema": {
                            "type": "OBJECT",
                            "properties": {
                                "is_non_policy": {"type": "BOOLEAN"},
                                "primary_australian_domain": {
                                    "type": "STRING",
                                    "enum": [
                                        "AU01", "AU02", "AU03", "AU04", "AU05",
                                        "AU06", "AU07", "AU08", "AU09", "AU10",
                                        "AU11", "AU12", "AU13", "AU14", "AU_OTHER_REVIEW"
                                    ],
                                    "nullable": True,
                                },
                                "secondary_australian_domains": {
                                    "type": "ARRAY",
                                    "items": {"type": "STRING"},
                                },
                                "reasoning": {"type": "STRING"},
                            },
                            "required": ["is_non_policy", "reasoning"],
                        },
                        "temperature": 0.0,
                    },
                )
                response = model.generate_content(user_prompt)
                raw_text = response.text or "{}"
                cleaned = clean_json_text(raw_text)
                return PolicyAnnotation.model_validate_json(cleaned)
            else:
                raise RuntimeError("No Google GenAI SDK available.")

        except Exception as err:
            if attempt == max_retries:
                raise RuntimeError(
                    f"Gemini API request failed after {max_retries} attempts: {err}"
                ) from err
            time.sleep(retry_delay * attempt)

    raise RuntimeError("Unexpected loop exit in classify_speech_with_retry")


def mock_classify_speech(record: Dict[str, Any]) -> PolicyAnnotation:
    """Mock classifier for offline dry-runs and pipeline testing."""
    vals = record.get("human_annotation", {}).get("values", {})
    # Return simulated prediction
    return PolicyAnnotation(
        is_non_policy=vals.get("is_non_policy", False),
        primary_australian_domain=vals.get("primary_australian_domain"),
        secondary_australian_domains=vals.get("secondary_australian_domains") or [],
        reasoning="[MOCK] Simulated prediction matching human annotation for pipeline test.",
    )


def compare_record(
    record: Dict[str, Any],
    prediction: PolicyAnnotation,
) -> Dict[str, Any]:
    """Compare single model output against human ground truth."""
    record_id = record.get("record_id", "unknown")
    human_vals = record.get("human_annotation", {}).get("values", {})

    human_is_non_policy = bool(human_vals.get("is_non_policy", False))
    human_primary = human_vals.get("primary_australian_domain")
    human_secondary = human_vals.get("secondary_australian_domains") or []

    model_is_non_policy = bool(prediction.is_non_policy)
    model_primary = prediction.primary_australian_domain
    model_secondary = prediction.secondary_australian_domains

    # Matches
    non_policy_match = (model_is_non_policy == human_is_non_policy)
    primary_domain_match = (model_primary == human_primary)
    full_match = (non_policy_match and primary_domain_match)

    speech = record.get("speech", {})
    speaker_name = speech.get("speaker_name", "")
    headings = " > ".join(filter(None, [speech.get("major_heading"), speech.get("minor_heading")]))
    snippet = (speech.get("text") or "")[:150].replace("\n", " ").strip()

    return {
        "record_id": record_id,
        "speech_metadata": {
            "speaker_name": speaker_name,
            "chamber": speech.get("chamber"),
            "date": speech.get("date"),
            "headings": headings,
        },
        "speech_snippet": snippet,
        "human": {
            "is_non_policy": human_is_non_policy,
            "primary_australian_domain": human_primary,
            "secondary_australian_domains": human_secondary,
        },
        "model": {
            "is_non_policy": model_is_non_policy,
            "primary_australian_domain": model_primary,
            "secondary_australian_domains": model_secondary,
            "reasoning": prediction.reasoning,
        },
        "matches": {
            "non_policy_match": non_policy_match,
            "primary_domain_match": primary_domain_match,
            "full_match": full_match,
        },
    }


def compute_metrics(comparisons: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate overall accuracy metrics and confusion matrices."""
    total = len(comparisons)
    if total == 0:
        return {"total": 0}

    non_policy_matches = sum(1 for c in comparisons if c["matches"]["non_policy_match"])
    primary_matches = sum(1 for c in comparisons if c["matches"]["primary_domain_match"])
    full_matches = sum(1 for c in comparisons if c["matches"]["full_match"])

    # Domain breakdown & confusion matrix
    all_domains = set()
    for c in comparisons:
        all_domains.add(str(c["human"]["primary_australian_domain"]))
        all_domains.add(str(c["model"]["primary_australian_domain"]))

    sorted_domains = sorted(all_domains)
    confusion_matrix: Dict[str, Dict[str, int]] = {
        h: {m: 0 for m in sorted_domains} for h in sorted_domains
    }

    domain_stats: Dict[str, Dict[str, Any]] = {
        d: {"human_count": 0, "model_count": 0, "true_positives": 0}
        for d in sorted_domains
    }

    for c in comparisons:
        h_dom = str(c["human"]["primary_australian_domain"])
        m_dom = str(c["model"]["primary_australian_domain"])
        confusion_matrix[h_dom][m_dom] += 1
        domain_stats[h_dom]["human_count"] += 1
        domain_stats[m_dom]["model_count"] += 1
        if h_dom == m_dom:
            domain_stats[h_dom]["true_positives"] += 1

    return {
        "total_samples": total,
        "non_policy_accuracy": round((non_policy_matches / total) * 100, 2),
        "primary_domain_accuracy": round((primary_matches / total) * 100, 2),
        "full_match_accuracy": round((full_matches / total) * 100, 2),
        "counts": {
            "non_policy_matches": non_policy_matches,
            "primary_domain_matches": primary_matches,
            "full_matches": full_matches,
            "total": total,
        },
        "domain_stats": domain_stats,
        "confusion_matrix": confusion_matrix,
    }


def print_summary_table(metrics: Dict[str, Any], comparisons: List[Dict[str, Any]]) -> None:
    """Print clean formatted CLI tables for evaluation results."""
    total = metrics["total_samples"]
    np_acc = metrics["non_policy_accuracy"]
    pd_acc = metrics["primary_domain_accuracy"]
    full_acc = metrics["full_match_accuracy"]
    counts = metrics["counts"]

    print("\n" + "=" * 80)
    print(" " * 22 + "EVALUATION SUMMARY REPORT")
    print("=" * 80)
    print(f"{'Metric':<35} | {'Score / Count':<20} | {'Percentage':<15}")
    print("-" * 80)
    print(f"{'Total Evaluated Samples':<35} | {total:<20} | 100.0%")
    np_ratio = f"{counts['non_policy_matches']}/{total}"
    pd_ratio = f"{counts['primary_domain_matches']}/{total}"
    full_ratio = f"{counts['full_matches']}/{total}"
    print(f"{'Non-Policy Flag Accuracy':<35} | {np_ratio:<20} | {np_acc:.1f}%")
    print(f"{'Primary Domain Exact Match':<35} | {pd_ratio:<20} | {pd_acc:.1f}%")
    print(f"{'Full Exact Match (Both)':<35} | {full_ratio:<20} | {full_acc:.1f}%")
    print("=" * 80)

    # Domain breakdown table
    print("\n" + "-" * 80)
    print("DOMAIN-LEVEL PERFORMANCE BREAKDOWN (Human vs Model)")
    print("-" * 80)
    print(f"{'Domain':<16} | {'Human':<8} | {'Model':<8} | {'Exact Matches':<14} | {'Recall':<10}")
    print("-" * 80)
    for dom, st in metrics["domain_stats"].items():
        if st["human_count"] == 0 and st["model_count"] == 0:
            continue
        recall = (
            f"{(st['true_positives'] / st['human_count']) * 100:.0f}%"
            if st["human_count"] > 0
            else "N/A"
        )
        dom_display = dom if dom != "None" else "None (Non-Policy)"
        print(
            f"{dom_display:<16} | {st['human_count']:<8} | {st['model_count']:<8} | "
            f"{st['true_positives']:<14} | {recall:<10}"
        )
    print("-" * 80)

    # Confusion matrix display
    cm = metrics.get("confusion_matrix", {})
    if cm:
        matrix_domains = sorted(cm.keys())
        col_headers = [d if len(d) <= 6 else d[:6] for d in matrix_domains]
        header_str = " | ".join(f"{h:>6}" for h in col_headers)
        divider_len = max(80, 18 + 9 * len(matrix_domains))

        print("\n" + "-" * divider_len)
        print("CONFUSION MATRIX (Rows: Human Ground Truth | Columns: Model Prediction)")
        print("-" * divider_len)
        print(f"{'Human \\ Model':<16} | {header_str}")
        print("-" * divider_len)
        for h_dom in matrix_domains:
            h_lbl = h_dom if h_dom != "None" else "None (NP)"
            row_str = " | ".join(f"{cm[h_dom].get(m_dom, 0):>6}" for m_dom in matrix_domains)
            print(f"{h_lbl:<16} | {row_str}")
        print("-" * divider_len)

    # Mismatches list
    mismatches = [c for c in comparisons if not c["matches"]["full_match"]]
    print(f"\nMISMATCHED SAMPLES ({len(mismatches)} of {total}):")
    print("=" * 80)

    if not mismatches:
        print("[SUCCESS] All evaluated samples matched human annotations exactly!")
    else:
        for idx, m in enumerate(mismatches, 1):
            h_np = m["human"]["is_non_policy"]
            h_dom = m["human"]["primary_australian_domain"]
            m_np = m["model"]["is_non_policy"]
            m_dom = m["model"]["primary_australian_domain"]
            rec_id = m["record_id"]
            meta = m["speech_metadata"]
            snippet = m["speech_snippet"]
            reasoning = m["model"]["reasoning"]

            print(f"[{idx}] Record ID: {rec_id}")
            print(f"    Speaker: {meta.get('speaker_name')} | Context: {meta.get('headings')}")
            print(f"    Snippet: \"{snippet}...\"")
            print(f"    Human: [non_policy={h_np}, primary_domain={h_dom}]")
            print(f"    Model: [non_policy={m_np}, primary_domain={m_dom}]")
            print(f"    Model Reasoning: {reasoning}")
            print("-" * 80)


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(
        description="Evaluate Australian Policy Annotation codebook draft against human annotations."
    )
    parser.add_argument(
        "--codebook",
        type=Path,
        default=repo_root
        / "scripts"
        / "conference-codebook-pilot-01-ai-codebook-input-d1d13e895705"
        / "CODEBOOK_CONTEXT.json",
        help="Path to CODEBOOK_CONTEXT.json",
    )
    parser.add_argument(
        "--annotations",
        type=Path,
        default=repo_root
        / "scripts"
        / "conference-codebook-pilot-01-ai-codebook-input-d1d13e895705"
        / "annotations.jsonl",
        help="Path to annotations.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "data" / "review" / "llm_codebook_test_results.json",
        help="Path to save raw evaluation results JSON",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=30,
        help="Number of records to evaluate (default: 30)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gemini-2.5-flash",
        help="Gemini model name (default: gemini-2.5-flash)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between API requests in seconds (default: 0.5s)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run evaluation with simulated mock responses without calling Gemini API",
    )

    args = parser.parse_args()

    # Step 1 & 2: Load Codebook context & sample records
    print("\n" + "=" * 80)
    print(" AUSTRALIAN PARLIAMENTARY SPEECH CODEBOOK EVALUATION")
    print("=" * 80)
    print(f"Codebook:    {args.codebook}")
    print(f"Annotations: {args.annotations}")
    print(f"Samples:     {args.samples}")
    print(f"Model:       {args.model}")
    print(f"Output File: {args.output}")

    if not args.codebook.is_file():
        print(f"\n[ERROR] Codebook file not found at: {args.codebook}", file=sys.stderr)
        sys.exit(1)

    if not args.annotations.is_file():
        print(f"\n[ERROR] Annotations file not found at: {args.annotations}", file=sys.stderr)
        sys.exit(1)

    print("\n[1/4] Loading codebook context and evaluation records...")
    codebook_context = load_codebook_context(args.codebook)
    records = load_annotations(args.annotations, max_records=args.samples)
    print(f"      Loaded codebook context and {len(records)} evaluation samples.")

    # Step 3: Construct structured system prompt
    print("\n[2/4] Constructing structured system prompt from codebook taxonomy...")
    system_prompt = build_system_prompt(codebook_context)
    print(f"      System prompt compiled ({len(system_prompt)} characters).")

    # Handle client initialization & API keys
    client = None
    if not args.mock:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print(
                "\n[ERROR] GEMINI_API_KEY environment variable is not set.",
                file=sys.stderr,
            )
            print(
                "Please configure your Gemini API key before running evaluations:\n"
                "  PowerShell (Windows): $env:GEMINI_API_KEY=\"your_api_key_here\"\n"
                "  Command Prompt:       set GEMINI_API_KEY=your_api_key_here\n"
                "  Bash (Linux/macOS):   export GEMINI_API_KEY=\"your_api_key_here\"\n"
                "  Or add GEMINI_API_KEY=your_api_key_here to a .env file.\n\n"
                "Tip: To verify the pipeline offline without API calls, run with '--mock'.",
                file=sys.stderr,
            )
            sys.exit(1)

        if not (HAVE_NEW_GENAI or HAVE_LEGACY_GENAI):
            print(
                "\n[ERROR] Neither 'google-genai' nor 'google-generativeai' package is installed.\n"
                "Please install the Google GenAI SDK: pip install google-genai",
                file=sys.stderr,
            )
            sys.exit(1)

        if HAVE_NEW_GENAI:
            client = genai.Client(api_key=api_key)
        elif HAVE_LEGACY_GENAI:
            legacy_genai.configure(api_key=api_key)

    # Step 4 & 5: Run classifications and comparisons
    print(f"\n[3/4] Evaluating {len(records)} samples against {args.model}...")
    comparisons: List[Dict[str, Any]] = []

    for i, record in enumerate(records, 1):
        rec_id = record.get("record_id", "unknown")
        user_prompt = build_speech_user_prompt(record)

        try:
            if args.mock:
                prediction = mock_classify_speech(record)
            else:
                prediction = classify_speech_with_retry(
                    client=client,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model_name=args.model,
                )
                if args.delay > 0:
                    time.sleep(args.delay)

            comp = compare_record(record, prediction)
            comparisons.append(comp)

            # Progress log
            match_str = "MATCH" if comp["matches"]["full_match"] else "MISMATCH"
            h_lbl = f"{comp['human']['primary_australian_domain'] or 'Non-Policy'}"
            m_lbl = f"{comp['model']['primary_australian_domain'] or 'Non-Policy'}"
            print(f"      [{i:02d}/{len(records)}] {rec_id[:25]}... -> Human: {h_lbl:<10} | Model: {m_lbl:<10} [{match_str}]")

        except Exception as err:
            print(f"      [{i:02d}/{len(records)}] ERROR on {rec_id}: {err}", file=sys.stderr)
            # Record failed sample
            comparisons.append({
                "record_id": rec_id,
                "error": str(err),
                "human": record.get("human_annotation", {}).get("values", {}),
                "model": None,
                "matches": {
                    "non_policy_match": False,
                    "primary_domain_match": False,
                    "full_match": False,
                },
            })

    # Step 6: Compute metrics & print summary
    metrics = compute_metrics(comparisons)
    print_summary_table(metrics, comparisons)

    # Step 7: Save raw comparison results
    print(f"\n[4/4] Saving raw evaluation results to {args.output}...")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    results_payload = {
        "metadata": {
            "evaluation_timestamp": datetime.now(timezone.utc).isoformat(),
            "model_name": args.model if not args.mock else "mock-simulator",
            "codebook_path": str(args.codebook),
            "annotations_path": str(args.annotations),
            "total_samples": len(comparisons),
            "is_mock": args.mock,
        },
        "metrics": metrics,
        "comparisons": comparisons,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results_payload, f, indent=2, ensure_ascii=False)

    print(f"      Successfully saved evaluation results to {args.output}\n")


if __name__ == "__main__":
    main()
