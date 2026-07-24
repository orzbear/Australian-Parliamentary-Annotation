# ADR: parliamentary interoperability boundary

Status: accepted for the Phase 2.5 foundation.

## Decision

Keep the internal canonical model centred on source documents, debate sections, speech
fragments, and speaker turns. OpenAustralia/PublicWhip-style XML is the first registered
source adapter. Target future ParlaMint/Parla-CLARIN compatibility through explicit
import/export mappings rather than making TEI XML the internal PostgreSQL schema or
rewriting the accepted pipeline now.

Source-specific metadata that lacks a shared representation remains in validated
attributes, JSON, and provenance fields. Mapping code must preserve identifiers,
ordering, speakers, language, dates, headings, interjections, lineage, uncertainty, and
source checksums, and must report lossy mappings.

## Consequences

The database remains efficient for annotation and preserves the accepted Australian
corpus. A later ParlaMint TEI adapter/exporter can map at the platform boundary and be
conformance-tested without forcing every source into TEI internally. This also permits
separately approved adapters for Akoma Ntoso, generic JSONL speech corpora, and generic
CSV speech corpora.

None of those adapters, nor TEI import/export, is implemented in Phase 2.5. Before adding
one, define its capability declaration, source/licensing contract, canonical mapping,
validation fixtures, round-trip expectations, and loss report.
