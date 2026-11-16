"""Reversible oracle-only names; frozen extractor and evaluator vocabularies stay intact."""
from __future__ import annotations

from copy import deepcopy

from .io import digest

PAIR_RENAMES = {'missing_detail':'missing_from_extraction', 'unsupported':'not_supported_by_abstract',
                'supporting':'not_material', 'cannot_establish':'cannot_establish_comparison',
                'no_effect':'effect_vs_no_effect', 'sample':'biological_sample'}
FIELD_NAMES = {'exposure':'exposure_or_condition', 'outcome':'measured_outcome', 'sign':'direction',
               'kind':'finding_type', 'comparator':'comparison_reference', 'context':'population_and_study_context'}
STRUCTURED_NAMES = {'comparison':'comparison_structure', 'population':'population_scope',
                    'sample_size':'sample_size_and_scope', 'analysis':'study_design_and_analysis',
                    'significance':'statistical_evidence'}
QUALITY = {'correct':'matches_abstract', 'incorrect':'does_not_match_abstract', 'uncertain':'cannot_judge_from_abstract'}
SUPPORT = {'supported':'supported_by_abstract', 'unsupported':'not_supported_by_abstract', 'uncertain':'cannot_judge_from_abstract'}
PROBLEMS = {'unsupported':'not_supported_by_abstract', 'missing_detail':'missing_from_extraction'}
OUTPUT_NAMES = {'claims':'claim_assessments', 'fields':'field_correctness', 'support':'source_support',
                'diagnostics':'structured_field_correctness', 'diagnostic_problems':'structured_field_problems',
                'note':'source_note', 'matched_result_ids':'matched_source_result_ids'}
INVENTORY_NAMES = {'results':'atomic_results', 'result_id':'source_result_id', 'anchors':'source_text_anchors',
                   'statement':'source_finding', 'evidence':'source_evidence', 'quote':'source_quote',
                   'sentence_id':'source_sentence_id', 'result_sentence_ids':'source_result_sentence_ids',
                   'multi_result_sentence':'shares_sentence_with_other_atomic_results',
                   'exposure':'exposure_or_condition', 'outcome':'measured_outcome',
                   'comparison':'comparison_contrast', 'subgroup':'population_subgroup',
                   'analysis':'analysis_method', 'time':'time_point'}
# Applied to private model views only. String values, IDs, source sentences and
# evidence quotations never change. Collisions are rejected rather than merged.
INPUT_NAMES = {'kind':'finding_type', 'sign':'direction', 'hedged':'stated_as_hypothesis',
               'n':'sample_size', 'total_n':'total_sample_size', 'sample':'biological_material',
               'significant':'authors_reported_statistical_significance',
               'result':'finding_result', 'assertion':'authors_finding_assertion',
               'context':'context_information', 'summary':'extraction'}


def reverse(mapping):
    result = {value:key for key,value in mapping.items()}
    if len(result) != len(mapping):raise ValueError('Name map is not reversible')
    return result


def rename_keys(value, mapping):
    if isinstance(value, dict):
        keys = [mapping.get(key,key) for key in value]
        if len(keys) != len(set(keys)):raise ValueError('Oracle field projection collides with an existing name')
        return {mapping.get(key,key):rename_keys(child,mapping) for key,child in value.items()}
    if isinstance(value, list):return [rename_keys(child,mapping) for child in value]
    return deepcopy(value)


def model_input_view(value):
    return rename_keys(value, INPUT_NAMES)


def restore_model_input(value):
    return rename_keys(value, reverse(INPUT_NAMES))


def projection_record(raw, displayed):
    if restore_model_input(displayed) != raw:
        raise ValueError('Model-facing naming projection changed source facts')
    return {'version':'oracle-input-names-5208dfa', 'names':dict(INPUT_NAMES),
            'raw_sha256':digest(raw), 'model_view_sha256':digest(displayed), 'values_unchanged':True}


def claim_to_wire(row):
    result = {'claim_id':row['claim_id'], 'field_correctness':{FIELD_NAMES[k]:QUALITY[v] for k,v in row['fields'].items()},
              'source_support':SUPPORT[row['support']], 'source_note':row['note']}
    if 'diagnostics' in row:
        result['structured_field_correctness'] = {STRUCTURED_NAMES[k]:QUALITY[v] for k,v in row['diagnostics'].items()}
        result['structured_field_problems'] = {STRUCTURED_NAMES[k]:[PROBLEMS.get(v,v) for v in values]
                                                for k,values in row['diagnostic_problems'].items()}
    if 'matched_result_ids' in row:result['matched_source_result_ids'] = deepcopy(row['matched_result_ids'])
    return result


def claim_from_wire(row):
    fields, quality, support = reverse(FIELD_NAMES), reverse(QUALITY), reverse(SUPPORT)
    result = {'claim_id':row['claim_id'], 'fields':{fields[k]:quality[v] for k,v in row['field_correctness'].items()},
              'support':support[row['source_support']], 'note':row['source_note']}
    if 'structured_field_correctness' in row:
        names, problems = reverse(STRUCTURED_NAMES), reverse(PROBLEMS)
        result['diagnostics'] = {names[k]:quality[v] for k,v in row['structured_field_correctness'].items()}
        result['diagnostic_problems'] = {names[k]:[problems.get(v,v) for v in values]
                                          for k,values in row['structured_field_problems'].items()}
    if 'matched_source_result_ids' in row:result['matched_result_ids'] = deepcopy(row['matched_source_result_ids'])
    return result


def extraction_to_wire(value):
    return {**{k:deepcopy(v) for k,v in value.items() if k not in {'claims','task'}},
            'task':'extraction_assessment', 'claim_assessments':[claim_to_wire(c) for c in value['claims']]}


def extraction_from_wire(value):
    return {**{k:deepcopy(v) for k,v in value.items() if k not in {'claim_assessments','task'}},
            'task':'extraction', 'claims':[claim_from_wire(c) for c in value['claim_assessments']]}


def rename_schema(schema, names, codes=None):
    """Change schema property names and constrained codes, never free text values."""
    result = deepcopy(schema)
    if 'properties' in result:
        result['properties'] = {names.get(k,k):rename_schema(v,names,None if k in {'claim_id','item_id'} else codes) for k,v in result['properties'].items()}
    if 'required' in result:result['required'] = [names.get(k,k) for k in result['required']]
    if 'items' in result:result['items'] = rename_schema(result['items'],names,codes)
    for keyword in ('oneOf','anyOf','allOf'):
        if keyword in result:result[keyword] = [rename_schema(x,names,codes) for x in result[keyword]]
    if codes:
        if 'enum' in result:result['enum'] = [codes.get(x,x) for x in result['enum']]
        if isinstance(result.get('const'),str):result['const'] = codes.get(result['const'],result['const'])
    return result


def judgment_schema(schema):
    return rename_schema(schema, {**OUTPUT_NAMES, **FIELD_NAMES, **STRUCTURED_NAMES},
                         {**QUALITY, **SUPPORT, **PROBLEMS, 'extraction':'extraction_assessment'})


FIELD_DESCRIPTIONS = {
    'instrument_version':'Copy the registered annotation-instrument version exactly.',
    'pipeline_version':'Copy the response package version; this does not identify which producer made an anonymous extraction.',
    'item_id':'Copy the supplied item identifier exactly; this is not a finding.',
    'task':'extraction_assessment means judging supplied claims, not generating a new extraction.',
    'claim_assessments':'One assessment for every supplied claim occurrence, including duplicates and errors.',
    'claim_id':'Copy the occurrence identifier exactly within its extraction.',
    'field_correctness':'Source fidelity of each of the six common extracted fields.',
    'exposure_or_condition':'The extracted exposure, intervention or disease/group condition, including molecule versus class.',
    'measured_outcome':'The extracted outcome and its measurement definition.',
    'direction':'Faithfulness of estimate orientation and the authors\' conclusion; not statistical significance alone.',
    'finding_type':'The extracted level, association, effect or risk type; association does not imply causality.',
    'comparison_reference':'The actual comparison group, reference or baseline/time contrast.',
    'population_and_study_context':'Population, place, design, count, duration, dose and biological material asserted in the extraction.',
    'source_support':'Whether the source evidence supports the finding as a whole, including direction and comparison.',
    'structured_field_correctness':'Additional source-fidelity checks for the richer structured extraction; not a medical diagnosis.',
    'structured_field_problems':'Problem codes only for structured fields marked does_not_match_abstract.',
    'comparison_structure':'Comparison type, groups, orientation and times.',
    'population_scope':'People, subgroup and analysis population to which the finding applies.',
    'sample_size_and_scope':'Count, unit and its whole-study/group/analysis scope.',
    'study_design_and_analysis':'Study design and analysis/adjustment, distinct from subgroup stratification.',
    'statistical_evidence':'Estimate, p-value/interval targets and the authors\' significance/trend wording.',
    'source_note':'Optional short source-grounded comment; not step-by-step reasoning.',
    'matched_source_result_ids':'Unique IDs of genuinely equivalent results in the shared source inventory; no duplicate recall credit.',
    'matches_abstract':'The extracted field is faithful to the source, including a justified empty field.',
    'does_not_match_abstract':'A substantive error or relevant omission relative to the source; not only errors that change a pair label.',
    'cannot_judge_from_abstract':'A genuinely ambiguous interpretation that cannot be resolved from the supplied abstract.',
    'supported_by_abstract':'The source supports the asserted finding as a whole.',
    'not_supported_by_abstract':'An asserted fact is contradicted or not supported by the abstract.',
    'missing_from_extraction':'Information or an exception stated in the abstract is absent from the extraction.',
}
INPUT_DESCRIPTIONS = {
    'study':'Extracted study-level design, aims and counts to check against the abstract, not authoritative extra source facts.',
    'publication_types':'Original PubMed metadata may support only the design it states, not result directions or a more specific design.',
    'evidence':'The extraction\'s cited sentence IDs and quotes; check both their presence and their support for the finding.',
    'arm':'Assigned extraction/sampling category; it is not evidence that a source assertion is correct.',
    'finding_type':'The extracted finding type: level, association, effect or risk.',
    'direction':'The saved direction: + means greater exposure relates to greater outcome; - lower; 0 explicitly no association/difference.',
    'stated_as_hypothesis':'The original cautious, uncertain or speculative assertion flag; the site label does not require a formally stated hypothesis.',
    'sample_size':'The extracted count value/object, preserving its original group and analysis scope.',
    'total_sample_size':'The extracted whole-study count, not a subgroup count.',
    'biological_material':'The extracted biological material, such as serum or plasma; never participant count.',
    'finding_result':'The extracted finding object, containing direction, conclusion and statistical evidence.',
    'authors_reported_statistical_significance':'The saved legacy statistical-significance assertion: true/false only when supplied by the source; null is extraction uncertainty.',
    'authors_finding_assertion':'The extracted authors\' conclusion; this remains an extraction to check.',
    'context_information':'The extracted context fields; missing information is not evidence of source absence.',
    'extraction':'The deterministic rendering of extracted fields; it is not an authoritative source verdict.',
    'value':'A value belonging to its containing count, estimate or statistic; its parent fixes the meaning.',
    'not_reported':'An explicit producer assertion that information is absent from the source; still requires checking.',
    'not_extracted':'The producer did not supply a value; this does not establish that the source omitted it.',
}
