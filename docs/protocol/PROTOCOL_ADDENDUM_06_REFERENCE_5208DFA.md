# Reference-only instrument alignment to site5208dfa

Authority: INBOX2026-11-14 04:15, superseding03:52. This amendment is registered before any oracle request and before any real C or human annotation exists. Astra remains held until explicit Antonio release.

The production/evaluation configuration remains SHA256 afff5c500863af50d1a40fa18af7c9a4d81b297f4bea5eae7adf8f78de8245ce. GLM extraction/classification prompts, schemas, request transforms, model/effort/caps, production outputs and bindings are unchanged.

The active generic oracle files are updated together with the versioned5208dfa copies. Dated24dd snapshots remain historical; explicit legacy runtime/snapshots support canonical decoding without importing the new generic aliases.

Final site export: commit5208dfa; SHA256 9ad34d2a75ebae868af7532122427982edd964166eecf41c4fe47a84eca44873. The deterministic tree and derived category/legacy-factor meanings are unchanged. Old wire answer codes are rejected. Biological sample continues to derive the legacy factor sample.

## Complete rename registry

### pair_answer_codes

- `missing_detail` → `missing_from_extraction`
- `unsupported` → `not_supported_by_abstract`
- `supporting` → `not_material`
- `cannot_establish` → `cannot_establish_comparison`
- `no_effect` → `effect_vs_no_effect`
- `sample` → `biological_sample`

### model_response_containers

- `claims` → `claim_assessments`
- `fields` → `field_correctness`
- `support` → `source_support`
- `diagnostics` → `structured_field_correctness`
- `diagnostic_problems` → `structured_field_problems`
- `note` → `source_note`
- `matched_result_ids` → `matched_source_result_ids`

### common_field_names

- `exposure` → `exposure_or_condition`
- `outcome` → `measured_outcome`
- `sign` → `direction`
- `kind` → `finding_type`
- `comparator` → `comparison_reference`
- `context` → `population_and_study_context`

### structured_field_names

- `comparison` → `comparison_structure`
- `population` → `population_scope`
- `sample_size` → `sample_size_and_scope`
- `analysis` → `study_design_and_analysis`
- `significance` → `statistical_evidence`

### model_correctness_codes

- `correct` → `matches_abstract`
- `incorrect` → `does_not_match_abstract`
- `uncertain` → `cannot_judge_from_abstract`

### model_support_codes

- `supported` → `supported_by_abstract`
- `unsupported` → `not_supported_by_abstract`
- `uncertain` → `cannot_judge_from_abstract`

### model_problem_codes

- `unsupported` → `not_supported_by_abstract`
- `missing_detail` → `missing_from_extraction`

### source_inventory_names

- `results` → `atomic_results`
- `result_id` → `source_result_id`
- `anchors` → `source_text_anchors`
- `statement` → `source_finding`
- `evidence` → `source_evidence`
- `quote` → `source_quote`
- `sentence_id` → `source_sentence_id`
- `result_sentence_ids` → `source_result_sentence_ids`
- `multi_result_sentence` → `shares_sentence_with_other_atomic_results`
- `exposure` → `exposure_or_condition`
- `outcome` → `measured_outcome`
- `comparison` → `comparison_contrast`
- `subgroup` → `population_subgroup`
- `analysis` → `analysis_method`
- `time` → `time_point`

### private_input_names

- `kind` → `finding_type`
- `sign` → `direction`
- `hedged` → `stated_as_hypothesis`
- `n` → `sample_size`
- `total_n` → `total_sample_size`
- `sample` → `biological_material`
- `significant` → `authors_reported_statistical_significance`
- `result` → `finding_result`
- `assertion` → `authors_finding_assertion`
- `context` → `context_information`
- `summary` → `extraction`

### task_and_comparison_names

- `output task extraction` → `extraction_assessment`
- `comparison task extraction_comparison` → `paired_extraction_assessment`
- `output inventory` → `source_result_inventory`
- `output variants` → `extraction_assessments`
- `input variants` → `candidate_extractions`

### visible_presentation_labels

- `Hedged wording` → `Stated as a hypothesis`
- `Sample` → `Biological material`
- `Region` → `Location`
- `renderer_version v2-en-3` → `v2-en-5208dfa`

## Frozen path replacements

| Path | Original SHA256 | Approved SHA256 |
|---|---|---|
| `src/chl_rag/v2_evaluate.py` | `248d7d4beefbdde173f780560e0d951cca632cfca9ab9d4d54d5f2a65c64ae06` | `9911c4bb675b77c37faef8149d5a6ae6b2faabf31036cd0aa41ffd7ea2b82258` |
| `src/chl_rag/v2_sampling.py` | `1a93f98beadf96ea7269297abdc0b4167429eb69648598ae9309588907c99056` | `948fee287ec2df8688ae98ebbcec62b6569481a91580798c8cca0d32d92c1a52` |
| `src/chl_rag/v2_reference_oracles.py` | `8331cccc0bd46ce0b0c0be33a6b1bbb1c387c07bc894d3116426c482e7d945e2` | `266b8777a1255219bb17355498e28ea1eb82c22b1b7c5568df04d5185754701f` |
| `configs/v2_instrument.json` | `9fa3b9d9c45af553fe3eb4c54078c1d133acda2e5f17dfca174e2a0b7b3f76f2` | `8b4309fd6dfbf41501e925afda7c903477225927e0e3196d2cb91eb2e5b08328` |
| `configs/v2_oracle_prompt.json` | `1f2854279562b539b6dc3ddeabd95f1c2132b726dcb92e2b7ff602a6946270c1` | `3df8f0678c03b959e24293cf856b4c026f946dc7d02c6071ce8fc11f67199639` |
| `configs/v2_pair_annotation_schema.json` | `24586a0641051eed153e7add6be3208130758fea16b46e97a7873c0e42230dff` | `cb879a4a197382cacb62251e946b1f50904211f6d7d8f2879606ec073d0bc0f2` |
| `configs/v2_extraction_annotation_schema.json` | `a2239b01478c79b69798f4ee306664215e14f8c808fa5243818cc03545a19a7c` | `d33976af28ae94c1f7f787d71a700276641153b79f998c648ee1b9ed0c1cdcdf` |
| `configs/v2_extraction_comparison_prompt.json` | `1f50faa05455963294b96489a951ebb20b3fd96d4e0d71c4fbd747fb06734e05` | `f99f1568a00933d1b6e73a6b721119f92ead388257159e37071242024ee17c63` |
| `src/chl_rag/v2_pair_instrument.py` | `267558d9e480e025be15eeb0e749c050f5a390fa6121f15fca5a3de92ed57002` | `b288142d1c4d1c5402069417ee5649998dbecc0d1e9998fa89f48674cac55fb6` |
| `src/chl_rag/v2_instrument.py` | `aa68dbd2def85b619816e9c53e5942b098298a1b6aceef5e031b5cfecf8b9598` | `8c0082982718fd5a25759601f8355736fd7698d4cd98c365d1dabe33a7e95670` |
| `src/chl_rag/v2_extraction_comparison.py` | `4f5aa57fbfbdde3a12177f8aae6979c8edb56a695eff275327157d1eece45b67` | `a42405d6de47e9d84151e8cf3ecc94d717efda9d6885aaf3169368d8d93145e1` |

## Compatibility, provenance and validation

The signed reference companion is separate from the immutable base configuration and operational transport companion. It binds exact approved files, original archives, this protocol and the independent wording review; it does not release inference. Its effective reference view updates only reference hashes/runtime paths in memory.

Package manifests, the D0 companion, oracle plans/results, student collection locks and author reference envelopes retain the original config hash and additionally bind the reference revision. Source sampling, item IDs/order, weights, six common fields, support and rich-field metrics, thresholds and bootstrap rules are unchanged. Model-only aliases invert to canonical data; the public human package is unchanged.

Generic-alias activation is tested in an isolated checkout with networking disabled. Both GLM request hashes and exact legacy instruction hashes match before/after activation. Parser AST equivalence plus branch/factor cases preserve deterministic labels. Rendered source values are unchanged even when they contain label-like strings. These checks do not prove identical LLM behavior or semantic accuracy on C.

The original author-UI raw records remain immutable. A private conversion boundary preserves raw responses/revisions and links them to evaluation envelopes; it requires explicit blinded-collection attestation. No participant answer or note enters model input or public logs.

The independent scientific review is results/v2/reference_wording_review_5208dfa.json (SHA256 d12f703f0d9ae21d99ab031bf1c21fa18254d7cbb18d04450cdc63c5c429cf30). The staged integration manifest is results/v2/reference_integration_proposal.json (SHA256 e58cd91a7720044f5a4d44a21a4f53dddb4c2050aa30295873d39c0d4fcf211b). Twenty-seven synthetic checks pass; no C/S/H source or participant response was read and no paid model call occurred.

Any later change requires a new registered revision. The approved activation is signed with its companion, reviewed files and original archives; the production binding is verified unchanged. An explicit oracle release must name the active reference revision.
