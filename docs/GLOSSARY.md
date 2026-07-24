# Product glossary

These terms are stable across the product. Existing Phase 2 table names remain unchanged.

- **Corpus:** a registered, bounded collection of parliamentary proceedings with common
  provenance, source format, chamber, language, date range, and publication policy.
- **Corpus version:** a reproducible corpus state identified by an accepted preprocessing
  run and its checksummed source and output manifests.
- **Source format:** the external representation supplied by a source.
- **Source adapter:** a small registered component that declares supported capabilities
  and delegates discovery, validation, and preprocessing for one source format.
- **Source document:** one immutable source file and its checksummed database version. A
  source document is an input member; a corpus is the registered collection containing
  many documents.
- **Debate section:** an ordered structural division within a source document.
- **Speech fragment:** one source-ordered extracted speech, continuation, interjection, or
  related unit retaining source attributes and text provenance.
- **Speaker turn:** the primary annotation unit: one conservatively reconstructed span
  attributable to a speaker. A turn may cite multiple fragments; interjections remain
  separate.
- **Taxonomy:** a named controlled classification system. A **codebook** is the broader
  methodological guidance for applying one or more taxonomies.
- **Taxonomy version:** an immutable-on-publication set of labels, definitions, hierarchy,
  flags, rules, examples, attribution, and a canonical content hash.
- **Taxonomy label:** one coded choice within a taxonomy version.
- **Annotation schema:** a reusable template describing a coherent annotation instrument.
- **Annotation schema version:** a checksummed, immutable-on-publication field-definition
  set. It says what can be recorded; it does not select data or assign people.
- **Annotation field:** one typed declarative input in a schema version.
- **Annotation project:** a future Phase 3 configuration that selects corpus turns, pins a
  schema and taxonomy versions, and defines workflow policy. A schema is reusable; a
  project is an operational study instance.
- **Assignment:** a future allocation of a project task to an annotator.
- **Annotation:** a future versioned human response to a project's schema for a turn.
- **Adjudication:** a future decision resolving disagreement while citing the annotation
  versions considered.
- **Enrichment:** additional derived or externally sourced information, such as CAP,
  political affiliation, or model output, kept distinct from source facts.
- **Export snapshot:** a future immutable, manifested selection of corpus, annotation, and
  enrichment versions for reproducible analysis.

**Source annotation** records information encoded by or directly observed in the source;
**enrichment** adds a later interpretation or external fact with separate provenance.
**Procedural content** concerns the operation of Parliament, such as a point of order.
**Government-operations policy** concerns substantive public policy about administration,
integrity, institutions, or public-service design. The latter is a policy domain; the
former is a content status.
