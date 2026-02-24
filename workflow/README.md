# Build Patient Cohort Workflow

Elastic Workflow that creates a normalized cohort index from multi-facility medical records.

## Setup

### Option A: Automatic (via setup.py)

```bash
python agent/setup.py
```

This imports the workflow YAML via `POST /api/workflows` and registers it as an Agent Builder tool.

### Option B: Manual (via Kibana UI)

1. Enable Workflows: Kibana → **Stack Management** → **Advanced Settings** → search `workflows:ui:enabled` → toggle ON
2. Go to **Workflows** in the left navigation
3. Click **Create workflow**
4. Paste the contents of `build_cohort.yaml`
5. Save and enable
6. Go to **Agent Builder** → **Tools** → **New tool**
7. Select type **Workflow** → choose "Build Patient Cohort"
8. Assign the tool to the Medical Cohort Agent

### Option C: API import

```bash
cat workflow/build_cohort.yaml | jq -Rs '{yaml: .}' | \
  curl -X POST "http://localhost:5601/api/workflows" \
    -u elastic:${ELASTIC_PASSWORD} \
    -H "kbn-xsrf: true" \
    -H "x-elastic-internal-origin: Kibana" \
    -H "Content-Type: application/json" \
    -d @-
```

**Note:** The `x-elastic-internal-origin: Kibana` header is required or you'll get a 404.

## What it does

Given structured criteria from the agent (conditions, age, gender, smoking, medications), facility field maps (`facilities` JSON), and a `search_text` for semantic matching:

1. **Deletes + creates** the `cohort_<name>` index with a normalized schema and ingest pipeline (steps 1-2)
2. **Strict pass** — Iterates over `facilities` via `foreach`. For each facility, runs `_reindex` with a single parameterized Painless script that reads field names from `params.f_*` and type hints from `params.t_*` (step 3)
3. **Semantic kNN pass** — Iterates over `knn_facilities` via nested `foreach`. For each facility, runs kNN search, then iterates over hits and indexes each into the cohort with field mapping from the outer facility config (step 4)
4. **Refresh + count** — Makes docs visible and returns breakdown by confidence and facility (steps 5-6)

Every indexed cohort document includes provenance fields to make the result auditable:
- `source_field_map`: which source fields were used for normalization
- `evidence_snippet`: auto-derived preview of `clinical_notes`
- `knn_score`: similarity score for kNN matches (probable only)

### How the generic Painless script works

Instead of per-facility Painless scripts, one script handles all facilities:

- `params.f_patient_id`, `params.f_age`, `params.f_gender`, etc. — source field names
- `params.t_patient_id` — type hint: `"keyword"` (direct), `"long"` (cast + zero-pad to 9), `"float"` (strip decimal + pad)
- `params.t_age` — type hint: `"integer"` (direct/parseInt), `"keyword_range"` (parse "60-70" → midpoint 65)
- `params.t_smoking` — type hint: `"boolean"` (compare as bool), `""` (field absent → "unknown")
- Empty `f_*` values = field not available → filter is skipped, output field gets default

The agent discovers facility schemas and builds the `facilities` JSON. Adding a new facility requires zero workflow changes.

## How kNN matching works

The strict pass filters on structured fields (conditions array, age, gender, smoking). But conditions mentioned only in clinical notes, or garbled by OCR, are missed.

The kNN pass:
1. Takes the original research question as `search_text`
2. Uses ES `query_vector_builder` to generate an E5 embedding via the `e5_embedder` inference endpoint
3. Iterates over `knn_facilities` — runs kNN search per facility to find the top-20 most semantically similar clinical notes
4. Nested `foreach` indexes each hit into the cohort with field mapping from the outer facility config
5. Uses `op_type: create` to skip docs already in the cohort from the strict pass

This catches what structured matching cannot:
- **OCR artifacts**: `סוכדת` instead of `סוכרת` (diabetes)
- **Synonyms**: "elevated glucose levels" without the word "diabetes"
- **Negation**: "not a smoker" vs "smoker" — kNN understands the semantic difference

### Known limitations of the kNN pass

- **Dedup with strict pass**: Both passes use `source_doc_id + '_' + source_index` as cohort doc ID. The kNN pass uses `op_type=create` so docs already indexed by the strict pass are skipped.
- **Empty search_text guard**: The entire kNN pass is wrapped in an `if` step that skips when `search_text` is blank.
- **Array fields concatenated**: Liquid templating joins arrays into strings (e.g., conditions become `"סוכרתיתר לחץ דם"` instead of `["סוכרת", "יתר לחץ דם"]`). The `clinical_notes` field is the primary source for probable matches — array fields are best-effort.
- **No text_embedding copied**: kNN hits don't include the embedding vector. Strict-pass docs retain their embeddings.
- **Shaked age is null in kNN**: Shaked's `age_group` is a string like "60-70". Liquid can't compute a midpoint, so `age` stays null for kNN hits. `age_raw` preserves the original range. The strict pass handles this correctly with Painless.

## Confidence levels

- **strict** — All criteria matched via structured fields. For facilities missing a requested field (e.g., smoking at Hadarim), `match_explanation` notes the caveat.
- **probable** — Matched via semantic similarity of clinical text, or from a facility with limited data (Ofek). Researcher should verify these manually.

## Inputs

| Parameter | Type | Required | Example |
|-----------|------|----------|---------|
| cohort_name | string | yes | `diabetic_smokers_60plus` |
| conditions | string (JSON array) | no | `["סוכרת סוג 2", "יתר לחץ דם"]` |
| age_min | string | no | `60` |
| age_max | string | no | `80` |
| gender | string | no | `male` or `female` |
| smoking | string | no | `true` or `false` |
| medications | string (JSON array) | no | `["מטפורמין"]` |
| icd10 | string (JSON array) | no | `["E11", "I10"]` |
| search_text | string | no | `חולי סוכרת מעל גיל 60 שמעשנים` |
| facilities | string (JSON array) | yes | Agent-built facility configs with `f_*`/`t_*` field maps |
| knn_facilities | string (JSON array) | no | Subset of facilities with `text_embedding` field |

The agent should **always** provide `search_text` (the original research question in Hebrew) — it powers the semantic kNN pass.

## Workflow steps

| # | Name | Type | Purpose |
|---|------|------|---------|
| 1 | delete_existing | elasticsearch.request | Delete previous cohort index (idempotent) |
| 1b | create_normalize_pipeline | elasticsearch.request | Create ingest pipeline for patient_id normalization |
| 2 | create_cohort_index | elasticsearch.request | Create index with normalized mapping + pipeline |
| 3 | reindex_facilities | foreach | Iterate facilities → reindex with single Painless script |
| 4 | run_knn_pass | if + foreach | Nested foreach: facilities × kNN hits → index normalized |
| 5 | refresh_cohort | elasticsearch.request | Refresh index for search visibility |
| 6 | count_results | elasticsearch.search | Count with confidence + facility breakdown + unique patients |

## Workflow YAML format

Uses the Elastic Workflows schema (Kibana 9.3+):
- Steps use `name`/`type`/`with` structure
- `elasticsearch.request` for generic ES API calls (_reindex, DELETE, PUT)
- `elasticsearch.search` for the final count aggregation (size/query/aggs directly under `with`, not inside `body`)
- Liquid templating: `{{ inputs.cohort_name }}`, `{{ foreach.item.f_patient_id }}`
- `foreach` iterates over `json_parse`d arrays: `{{ inputs.facilities | json_parse }}`
- Nested foreach: inner uses `foreach.item`, outer uses `steps.<outer_name>.item`
- `if` step with Liquid condition: `{% if inputs.search_text != blank %}true{% else %}false{% endif %}`
- Error handling: `on-failure: continue: true` for idempotent re-runs
- Painless empty `if` bodies need `int _skip = 0;` (block comments cause parse errors)
