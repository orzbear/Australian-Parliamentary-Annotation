# Future CAP integration plan

CAP is not imported, seeded, or required in Phase 2.5. The initial Australian annotation
schema contains no CAP fields.

A later, separately approved enrichment would create a versioned CAP taxonomy with major
topics as parents and subtopics as children. Human or model input would store only the
selected subtopic; the major topic would be derived through the same-version parent
relationship. Official codes, labels, definitions, examples, attribution, source URL,
licence note, and content hash would be loaded from an authorised source.

Optional `maps_to` relations could connect CAP subtopics to Australian policy-domain
labels without changing either taxonomy. Relations are crosswalk evidence, not identity,
and require review notes. A new external release becomes a new taxonomy version;
published rows are never edited.

Before implementation the owner must approve the source, permitted use and
redistribution, attribution wording, version, crosswalk method, review policy, and whether
CAP is human-coded or model-derived. No placeholder or copied CAP content should enter
the repository before that approval.
