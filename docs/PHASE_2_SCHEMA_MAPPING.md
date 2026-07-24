# Phase 2 schema mapping

Status: pre-import mapping approved from the accepted Phase 1 artefacts on
23 July 2026.

## Evidence and mapping rules

The source for this mapping is
`data/processed/phase1-full-20260723-v4/`, not the illustrative field lists in
the Phase 2 brief. All 259 files listed by `run_manifest.json` were present and
their SHA-256 and byte size matched. The manifest SHA-256 is
`1d7698829d9d1cdceb0373c5d2fc55a719f2a43e6d5242e38bd99067215680df`.
The manifest records 926 source files, 784,583,349 source bytes, and inventory
SHA-256
`7121a0fb10676b7bd68d582c5985671b71343ded51343b3f5f2b35bb474144e2`.

Every Parquet partition of a dataset has one consistent physical schema. All
Arrow fields are physically marked nullable, so the `Nullable?` column below
describes the PostgreSQL contract after checking the accepted values and Phase 1
semantics. `No` means the accepted corpus contains no null and the value is part
of the record contract. Empty source strings remain empty strings; the importer
does not silently convert them to null.

The importer adds a canonical `record_sha256` to immutable domain records. It is
SHA-256 over canonical JSON of the complete Phase 1 row before relational key
resolution. String-encoded JSON is parsed and stored as JSONB without changing
its values. Arrow `list<string>` becomes `text[]`. Deterministic Phase 1 keys
remain unique domain identifiers; PostgreSQL identity columns are the relational
primary keys.

## Differences from the suggested Phase 2 fields

- `speaker_turns` has no `start_time_raw`, `parsed_start_time`, or
  `is_continuation_merged` column. Phase 2 does not invent them. Merged
  continuations remain exactly observable through ordered lineage and
  `fragment_count`.
- `debate_sections` has no business classification fields. Phase 2 does not
  infer them.
- `speaker_turns` has original heading text but no section keys. Section foreign
  keys are resolved deterministically through the first fragment's Phase 1
  section keys; the original heading columns are retained.
- `debate_events` has original heading text but no section keys. Its optional
  section references are resolved to the latest applicable Phase 1 section in
  the same source file and sequence scope; original headings remain authoritative.
- Image anomalies are not Parquet. The four accepted rows are in the manifested
  `reports/image_elements.csv`.
- The accepted physical `parsed_time` type is `time32[ms]` (despite the current
  source schema declaration showing seconds), so PostgreSQL uses `time(3)`.
- `attributes_json`, `payload_json`, and `block_structure_json` are Parquet
  strings containing JSON; PostgreSQL stores validated JSONB.
- Phase 1 nullable booleans remain nullable. In particular, all
  `is_presiding_officer`, `is_question`, and `is_answer` values are unknown.

## Parquet datasets

### `source_files` (926 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `source_file_key` | string | `source_file_versions` | `source_file_key` | text | No | No | Unique Phase 1 domain key. |
| `source_file` | string | `source_files` | `relative_path` | text | No | Rename | Validated safe POSIX relative path. |
| `source_file_sha256` | string | `source_file_versions` | `source_sha256` | char(64) | No | Rename | Hex check. |
| `byte_size` | int64 | `source_file_versions` | `byte_size` | bigint | No | No | Nonnegative check. |
| `modification_time_ns` | int64 | `source_file_versions` | `modification_time_ns` | bigint | No | No | Evidence only; not content identity. |
| `date` | date32[day] | `source_files` | `speech_date` | date | No | Rename | Must match path year. |
| `chamber` | string | `source_files` | `chamber` | text | No | No | Accepted value is retained verbatim. |

`source_files.folder_year` is safely derived from the validated first path
component. `source_file_versions.preprocessing_run_id`, status, reconciliation,
and source-URL-marker validity come from the manifest and
`file_processing_report.csv`.

### `debate_sections` (97,975 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `section_key` | string | `debate_sections` | `section_key` | char(64) | No | No | Unique deterministic key. |
| `source_file` | string | `debate_sections` | `source_file_version_id` | bigint | No | Resolve FK | Match run, path, and checksum. |
| `source_file_sha256` | string | `debate_sections` | FK validation | — | No | Validate | Must equal resolved source version. |
| `sequence_number` | int32 | `debate_sections` | `sequence_number` | integer | No | No | Positive ordered source sequence. |
| `section_type` | string | `debate_sections` | `section_level` | text | No | Rename | Check: `major`, `minor`. |
| `parent_section_key` | string | `debate_sections` | `parent_section_id` | bigint | Yes | Resolve FK | Null for all 19,973 major headings. |
| `heading_original` | string | `debate_sections` | `heading_original` | text | No | No | Empty strings preserved. |
| `heading_clean` | string | `debate_sections` | `heading_normalised` | text | No | Rename | Phase 1 conservative clean heading. |
| `source_fragment_id` | string | `debate_sections` | `source_heading_id` | text | No | Rename | Empty source IDs remain empty. |
| `source_url` | string | `debate_sections` | `source_url` | text | No | No | Empty values remain empty. |
| `warning_codes` | list<string> | `debate_sections` | `warning_codes` | text[] | No | Arrow list | Order retained. |

### `debate_events` (14,352 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `event_key` | string | `debate_events` | `event_key` | char(64) | No | No | Unique deterministic key. |
| `source_file` | string | `debate_events` | `source_file_version_id` | bigint | No | Resolve FK | Run-scoped source version. |
| `source_file_sha256` | string | `debate_events` | FK validation | — | No | Validate | Must equal source version. |
| `sequence_number` | int32 | `debate_events` | `sequence_number` | integer | No | No | Unique per source file. |
| `date` | date32[day] | `debate_events` | `speech_date` | date | No | Rename | Retained for exact querying. |
| `chamber` | string | `debate_events` | `chamber` | text | No | No | No inference. |
| `element_type` | string | `debate_events` | `event_type` | text | No | Rename | Accepted values: `bills`, `division`. |
| `source_fragment_id` | string | `debate_events` | `source_event_id` | text | Yes | Rename | Null for 11,504 bills events. |
| `major_heading_original` | string | `debate_events` | `major_heading_original` | text | No | No | Also used to resolve optional section FK. |
| `minor_heading_original` | string | `debate_events` | `minor_heading_original` | text | No | No | Empty values preserved. |
| `source_url` | string | `debate_events` | `source_url` | text | Yes | No | Null for 11,504 bills events. |
| `attributes_json` | string | `debate_events` | `attributes` | jsonb | No | Parse JSON | Exact JSON value. |
| `payload_json` | string | `debate_events` | `payload` | jsonb | No | Parse JSON | Nested bills/division content is not flattened. |
| `bill_record_count` | int32 | `debate_events` | `bill_record_count` | integer | No | No | Nonnegative. |
| `warning_codes` | list<string> | `debate_events` | `warning_codes` | text[] | No | Arrow list | Order retained. |

### `speech_fragments` (269,793 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `fragment_key` | string | `speech_fragments` | `fragment_key` | char(64) | No | No | Unique deterministic key. |
| `source_fragment_id` | string | `speech_fragments` | `source_fragment_id` | text | No | No | Empty values preserved. |
| `source_file` | string | `speech_fragments` | `source_file_version_id` | bigint | No | Resolve FK | Run, path, and hash must agree. |
| `source_file_sha256` | string | `speech_fragments` | FK validation | — | No | Validate | Not duplicated after FK resolution. |
| `fragment_projection_sha256` | string | `speech_fragments` | `fragment_projection_sha256` | char(64) | No | No | Phase 1 projection checksum. |
| `sequence_number` | int32 | `speech_fragments` | `sequence_number` | integer | No | No | Unique per source version. |
| `date` | date32[day] | `speech_fragments` | `speech_date` | date | No | No | Must match source file date. |
| `chamber` | string | `speech_fragments` | `chamber` | text | No | No | Must match source file chamber. |
| `major_section_key` | string | `speech_fragments` | `major_section_id` | bigint | No | Resolve FK | All accepted values resolve. |
| `minor_section_key` | string | `speech_fragments` | `minor_section_id` | bigint | No | Resolve FK | Empty key maps to null; heading text stays exact. |
| `major_heading_original` | string | `speech_fragments` | `major_heading_original` | text | No | No | Exact Phase 1 value. |
| `minor_heading_original` | string | `speech_fragments` | `minor_heading_original` | text | No | No | Empty value preserved. |
| `speaker_id_raw` | string | `speech_fragments` | `speaker_id_raw` | text | Yes | No | 1,738 null. |
| `speaker_name_raw` | string | `speech_fragments` | `speaker_name_raw` | text | Yes | No | 1,738 null. |
| `talktype_raw` | string | `speech_fragments` | `talktype_raw` | text | No | No | Check accepted `speech`, `continuation`, `interjection`. |
| `time_raw` | string | `speech_fragments` | `time_raw` | text | No | No | Empty/malformed values preserved. |
| `parsed_time` | time32[ms] | `speech_fragments` | `parsed_time` | time(3) | Yes | Arrow time | 4,418 null. |
| `approximate_wordcount_raw` | string | `speech_fragments` | `approximate_wordcount_raw` | text | No | No | Raw source value. |
| `approximate_duration_raw` | string | `speech_fragments` | `approximate_duration_raw` | text | No | No | Raw source value. |
| `source_url` | string | `speech_fragments` | `source_url` | text | No | No | Empty values preserved. |
| `nospeaker_raw` | string | `speech_fragments` | `nospeaker_raw` | text | Yes | No | 268,055 null. |
| `attributes_json` | string | `speech_fragments` | `attributes` | jsonb | No | Parse JSON | Original XML attributes. |
| `text_raw` | string | `speech_fragments` | `text_raw` | text | No | No | Immutable Phase 1 raw extraction. |
| `text_clean` | string | `speech_fragments` | `text_clean` | text | No | No | Immutable conservative clean text. |
| `calculated_word_count` | int32 | `speech_fragments` | `calculated_word_count` | integer | No | No | Nonnegative. |
| `block_structure_json` | string | `speech_fragments` | `block_structure` | jsonb | No | Parse JSON | Exact structured extraction. |
| `parse_status` | string | `speech_fragments` | `parse_status` | text | No | No | All accepted rows are `parsed`. |
| `warning_codes` | list<string> | `speech_fragments` | `warning_codes` | text[] | No | Arrow list | No warning is dropped. |
| `boundary_reason_before` | string | `speech_fragments` | `boundary_reason_before` | text | Yes | No | 190,412 null. |
| `business_type` | string | `speech_fragments` | `business_type` | text | Yes | No | 57,674 unknown. |
| `business_mapping_version` | string | `speech_fragments` | `business_mapping_version` | text | No | No | Version retained even when type is unknown. |
| `is_presiding_officer` | bool | `speech_fragments` | `is_presiding_officer` | boolean | Yes | No | All values unknown. |
| `is_collective_speaker` | bool | `speech_fragments` | `is_collective_speaker` | boolean | Yes | No | 3,112 unknown. |
| `is_procedural` | bool | `speech_fragments` | `is_procedural` | boolean | Yes | No | 73,113 unknown. |
| `is_ceremonial` | bool | `speech_fragments` | `is_ceremonial` | boolean | Yes | No | 58,075 unknown. |
| `is_division_related` | bool | `speech_fragments` | `is_division_related` | boolean | Yes | No | 57,674 unknown. |
| `is_question_time` | bool | `speech_fragments` | `is_question_time` | boolean | Yes | No | 57,674 unknown. |
| `is_question` | bool | `speech_fragments` | `is_question` | boolean | Yes | No | All values unknown. |
| `is_answer` | bool | `speech_fragments` | `is_answer` | boolean | Yes | No | All values unknown. |
| `is_interjection` | bool | `speech_fragments` | `is_interjection` | boolean | No | No | Phase 1 classification. |
| `is_continuation_merged` | bool | `speech_fragments` | `is_continuation_merged` | boolean | No | No | Exactly 30,686 true. |
| `is_orphan_continuation` | bool | `speech_fragments` | `is_orphan_continuation` | boolean | No | No | Exactly 8,239 true. |
| `has_known_speaker` | bool | `speech_fragments` | `has_known_speaker` | boolean | No | No | No null-to-false coercion. |
| `eligible_50_words` | bool | `speech_fragments` | `eligible_50_words` | boolean | No | No | Phase 1 output only. |
| `eligible_100_words` | bool | `speech_fragments` | `eligible_100_words` | boolean | No | No | Phase 1 output only. |
| `eligible_main_analysis` | bool | `speech_fragments` | `eligible_main_analysis` | boolean | Yes | No | 73,514 unknown. |

### `speaker_turns` (160,577 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `turn_key` | string | `speaker_turns` | `turn_key` | char(64) | No | No | Unique deterministic key. |
| `source_file` | string | `speaker_turns` | `source_file_version_id` | bigint | No | Resolve FK | Run-scoped source version. |
| `source_file_sha256` | string | `speaker_turns` | FK validation | — | No | Validate | Must equal source version. |
| `turn_sequence` | int32 | `speaker_turns` | `turn_sequence` | integer | No | No | Unique per reconstruction/source version. |
| `date` | date32[day] | `speaker_turns` | `speech_date` | date | No | No | Exact Phase 1 value. |
| `chamber` | string | `speaker_turns` | `chamber` | text | No | No | Exact Phase 1 value. |
| `first_fragment_sequence` | int32 | `speaker_turns` | `first_fragment_sequence` | integer | No | No | Used with lineage to resolve sections. |
| `last_fragment_sequence` | int32 | `speaker_turns` | `last_fragment_sequence` | integer | No | No | Must be at least first sequence. |
| `major_heading_original` | string | `speaker_turns` | `major_heading_original` | text | No | No | Empty strings preserved. |
| `minor_heading_original` | string | `speaker_turns` | `minor_heading_original` | text | No | No | Empty strings preserved. |
| `speaker_id_raw` | string | `speaker_turns` | `speaker_id_raw` | text | Yes | No | 1,738 null. |
| `speaker_name_raw` | string | `speaker_turns` | `speaker_name_raw` | text | Yes | No | 1,738 null. |
| `text_raw` | string | `speaker_turns` | `text_raw` | text | No | No | Interjection text remains excluded. |
| `text_clean` | string | `speaker_turns` | `text_clean` | text | No | No | Interjection text remains excluded. |
| `calculated_word_count` | int32 | `speaker_turns` | `calculated_word_count` | integer | No | No | Nonnegative. |
| `fragment_count` | int32 | `speaker_turns` | `fragment_count` | integer | No | No | Positive; reconciled to lineage. |
| `business_type` | string | `speaker_turns` | `business_type` | text | Yes | No | 44,249 unknown. |
| `business_mapping_version` | string | `speaker_turns` | `business_mapping_version` | text | No | No | Version retained. |
| `warning_codes` | list<string> | `speaker_turns` | `warning_codes` | text[] | No | Arrow list | Order retained. |
| `is_presiding_officer` | bool | `speaker_turns` | `is_presiding_officer` | boolean | Yes | No | All values unknown. |
| `is_collective_speaker` | bool | `speaker_turns` | `is_collective_speaker` | boolean | Yes | No | 3,092 unknown. |
| `is_procedural` | bool | `speaker_turns` | `is_procedural` | boolean | Yes | No | 53,844 unknown. |
| `is_ceremonial` | bool | `speaker_turns` | `is_ceremonial` | boolean | Yes | No | 44,538 unknown. |
| `is_division_related` | bool | `speaker_turns` | `is_division_related` | boolean | Yes | No | 44,249 unknown. |
| `is_question_time` | bool | `speaker_turns` | `is_question_time` | boolean | Yes | No | 44,249 unknown. |
| `is_question` | bool | `speaker_turns` | `is_question` | boolean | Yes | No | All values unknown. |
| `is_answer` | bool | `speaker_turns` | `is_answer` | boolean | Yes | No | All values unknown. |
| `is_interjection` | bool | `speaker_turns` | `is_interjection` | boolean | No | No | Always false for accepted turns. |
| `is_orphan_continuation` | bool | `speaker_turns` | `is_orphan_continuation` | boolean | No | No | Exactly 8,239 true. |
| `interrupted` | bool | `speaker_turns` | `interrupted` | boolean | No | No | Must agree with interruption count. |
| `interruption_count` | int32 | `speaker_turns` | `interruption_count` | integer | No | No | Nonnegative. |
| `has_known_speaker` | bool | `speaker_turns` | `has_known_speaker` | boolean | No | No | Exact Phase 1 flag. |
| `eligible_50_words` | bool | `speaker_turns` | `eligible_50_words` | boolean | No | No | Exact Phase 1 flag. |
| `eligible_100_words` | bool | `speaker_turns` | `eligible_100_words` | boolean | No | No | Exact Phase 1 flag. |
| `eligible_main_analysis` | bool | `speaker_turns` | `eligible_main_analysis` | boolean | Yes | No | 54,133 unknown. |

`reconstruction_run_id` is supplied from the manifest's reconstruction version
and configuration hashes. `major_section_id` and `minor_section_id` are resolved
through the first ordered lineage fragment, without modifying any turn value.

### `speaker_turn_fragments` (191,263 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `turn_key` | string | `speaker_turn_fragments` | `turn_id` | bigint | No | Resolve FK | Must resolve in the same reconstruction run. |
| `fragment_key` | string | `speaker_turn_fragments` | `fragment_id` | bigint | No | Resolve FK | Must resolve in the same preprocessing run. |
| `ordinal` | int32 | `speaker_turn_fragments` | `ordinal` | integer | No | No | Phase 1 is one-based; positive check. |
| `relation_type` | string | `speaker_turn_fragments` | `relation_type` | text | No | No | `initial`, `continuation`, `orphan_continuation`. |
| `merge_reason_code` | string | `speaker_turn_fragments` | `merge_reason_code` | text | No | No | All six accepted reasons retained. |
| `source_file` | string | `speaker_turn_fragments` | compatibility validation | — | No | Validate | Must equal turn and fragment source. |
| `fragment_sequence` | int32 | `speaker_turn_fragments` | `fragment_sequence` | integer | No | No | Retained and checked against fragment. |

The relational primary key is `(turn_id, ordinal)` and `(turn_id, fragment_id)`
is unique. A canonical `record_sha256` is retained for conflict detection.

### `interjections` (78,530 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `interjection_key` | string | `interjections` | `interjection_key` | char(64) | No | No | Unique deterministic key. |
| `source_fragment_key` | string | `interjections` | `fragment_id` | bigint | No | Resolve FK | Unique; prohibited from turn lineage. |
| `source_file` | string | `interjections` | `source_file_version_id` | bigint | No | Resolve FK | Must match fragment source. |
| `source_file_sha256` | string | `interjections` | FK validation | — | No | Validate | Must match source version. |
| `sequence_number` | int32 | `interjections` | `sequence_number` | integer | No | No | Must match fragment sequence. |
| `date` | date32[day] | `interjections` | `speech_date` | date | No | No | Exact Phase 1 value. |
| `chamber` | string | `interjections` | `chamber` | text | No | No | Exact Phase 1 value. |
| `speaker_id_raw` | string | `interjections` | `speaker_id_raw` | text | No | No | Empty string remains empty. |
| `speaker_name_raw` | string | `interjections` | `speaker_name_raw` | text | No | No | Empty string remains empty. |
| `is_collective` | bool | `interjections` | `is_collective` | boolean | Yes | No | 20 unknown. |
| `text_raw` | string | `interjections` | `text_raw` | text | No | No | Never merged into turn text. |
| `text_clean` | string | `interjections` | `text_clean` | text | No | No | Never merged into turn text. |
| `interrupted_turn_key` | string | `interjections` | `interrupted_turn_id` | bigint | Yes | Resolve FK | 968 unlinked. |
| `link_confidence` | string | `interjections` | `link_confidence` | text | No | No | `high` or `none`. |
| `link_reason` | string | `interjections` | `link_reason` | text | No | No | Exact Phase 1 reason. |
| `major_heading_original` | string | `interjections` | `major_heading_original` | text | No | No | Empty strings preserved. |
| `minor_heading_original` | string | `interjections` | `minor_heading_original` | text | No | No | Empty strings preserved. |
| `warning_codes` | list<string> | `interjections` | `warning_codes` | text[] | No | Arrow list | Order retained. |

`reconstruction_run_id` is added from the accepted reconstruction identity.

### `continuation_anomalies` (8,239 rows)

| Parquet column | Parquet type | PostgreSQL table | PostgreSQL column | PostgreSQL type | Nullable? | Transformation required? | Notes |
|---|---|---|---|---|---|---|---|
| `anomaly_key` | string | `continuation_anomalies` | `anomaly_key` | char(64) | No | No | Unique deterministic key. |
| `fragment_key` | string | `continuation_anomalies` | `fragment_id` | bigint | No | Resolve FK | One anomaly per orphan fragment. |
| `source_file` | string | `continuation_anomalies` | source compatibility | — | No | Validate | Must match fragment source. |
| `sequence_number` | int32 | `continuation_anomalies` | `sequence_number` | integer | No | No | Must match fragment sequence. |
| `source_fragment_id` | string | `continuation_anomalies` | `source_fragment_id` | text | No | No | Empty string preserved. |
| `speaker_id_raw` | string | `continuation_anomalies` | `speaker_id_raw` | text | No | No | Empty string preserved. |
| `speaker_name_raw` | string | `continuation_anomalies` | `speaker_name_raw` | text | No | No | Empty string preserved. |
| `candidate_turn_key` | string | `continuation_anomalies` | `candidate_turn_id` | bigint | Yes | Resolve FK | Null for 97 no-candidate anomalies. |
| `candidate_speaker_id_raw` | string | `continuation_anomalies` | `candidate_speaker_id_raw` | text | Yes | No | Same 97 null. |
| `candidate_speaker_name_raw` | string | `continuation_anomalies` | `candidate_speaker_name_raw` | text | Yes | No | Same 97 null. |
| `reason_code` | string | `continuation_anomalies` | `reason_code` | text | No | No | Accepted counts preserved exactly. |
| `major_heading_original` | string | `continuation_anomalies` | `major_heading_original` | text | No | No | Empty string preserved. |
| `minor_heading_original` | string | `continuation_anomalies` | `minor_heading_original` | text | No | No | Empty string preserved. |

`generated_turn_id` is resolved from the orphan lineage row, and the accepted
preprocessing/reconstruction run IDs are added. The four reason counts are 7,050
known-ID mismatch, 1,089 missing/ambiguous identity, 97 no active candidate, and
3 division boundary.

## Manifest and report artefacts

| Input | Input field | PostgreSQL table | PostgreSQL column/type | Nullable? | Transformation and notes |
|---|---|---|---|---|---|
| `run_manifest.json` | whole document | `preprocessing_runs` | `manifest jsonb` | No | Validated object, stored in full. |
| manifest | `run_id` | `preprocessing_runs` | `run_name text` | No | Accepted import identity component. |
| manifest | `pipeline_version` | `preprocessing_runs` | `pipeline_version text` | No | Exact value. |
| manifest | `git_commit` | `preprocessing_runs` | `git_commit text` | Yes | Empty/unknown is retained as null. |
| manifest | `input_inventory_sha256` | `preprocessing_runs` | `inventory_sha256 char(64)` | No | Must equal accepted value. |
| manifest | canonical file bytes | `preprocessing_runs` | `manifest_sha256 char(64)` | No | SHA-256 of exact manifest bytes. |
| manifest | `configuration_hashes` | `preprocessing_runs` | `configuration_hashes jsonb` | No | Exact mapping. |
| manifest | `source_files` totals | `preprocessing_runs` | file/byte counts | No | Count and sum, cross-checked. |
| manifest | start/completion timestamps | `preprocessing_runs` | timestamptz columns | No | ISO-8601 parse. |
| manifest | reconstruction version/hashes | `reconstruction_runs` | version/hash columns | No | One accepted reconstruction identity. |
| manifest | each `output_files` entry | `import_artifacts` | path/type/SHA/bytes | No | All 259 entries recorded. |
| `run_manifest.json` | file itself | `import_artifacts` | manifest artefact row | No | Additional artefact with computed SHA/bytes. |
| each Parquet artefact | footer schema/rows | `import_artifacts` | `parquet_schema jsonb`, `row_count bigint` | Yes | Metadata only; no full-file materialisation. |
| each CSV report | header/rows | `import_artifacts` | report metadata/count | Yes | Manifest checksum remains authoritative. |
| `file_processing_report.csv` | status/error/count/reconciled fields | `source_file_versions` | status and reconciliation fields | Mixed | Join by path and SHA; all accepted files are completed and reconciled. |
| `file_processing_report.csv` | before/after mtime | `source_file_versions` | mtime evidence columns bigint | No | Must agree and match source dataset. |
| `source_url_exceptions.csv` | absence of rows | `source_file_versions` | `source_url_marker_valid boolean` | No | True only after validated zero exceptions. |
| `image_elements.csv` | `image_key` | `image_anomalies` | `image_key char(64)` | No | Unique deterministic key. |
| `image_elements.csv` | `fragment_key` | `image_anomalies` | `fragment_id bigint` | No | Resolve FK; exactly two fragments. |
| `image_elements.csv` | `source_file`, `sequence_number` | `image_anomalies` | trace fields | No | Validate against fragment. |
| `image_elements.csv` | `image_index` | `image_anomalies` | `element_ordinal integer` | No | Positive. |
| `image_elements.csv` | `attributes_json` | `image_anomalies` | `element_attributes jsonb` | No | Parse exact JSON; no remote retrieval. |
| `image_elements.csv` | `alt_text` | `image_anomalies` | `alt_text text` | No | Accepted empty strings retained. |
| `image_elements.csv` | `clean_text_replacement` | `image_anomalies` | `clean_text_replacement text` | No | All four are `[IMAGE]`. |
| `image_elements.csv` | headings | `image_anomalies` | original heading text | No | Trace evidence retained. |

Other reports are imported as checksum-proven artefacts. Their evidence is
reconciled against final tables rather than duplicated into separate operational
tables: continuation anomalies duplicate the Parquet dataset exactly;
malformed/duplicate/reconciliation/source-URL exception reports have zero rows;
the corpus summary, unknown headings, and missing speakers are reproducible
queries over accepted rows.

## Relational-only tables and views

- `database_import_runs` records every running, completed, reused, failed, or
  rolled-back attempt without credentials or full database URLs.
- `preprocessing_runs`, `reconstruction_runs`, and every accepted corpus row are
  protected from update/delete after acceptance.
- `annotation_ready_turns`, `turns_with_fragment_counts`, and
  `corpus_year_summary` are read-only views. They expose stored values and
  lineage counts; they do not infer political metadata or hide null/anomaly
  values.

This mapping is internally consistent with the accepted artefacts and is the
contract against which the dry-run validator and PostgreSQL staging importer are
implemented.
