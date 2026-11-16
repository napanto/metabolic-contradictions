"""Signed reference-only view of the original v2 production configuration.

The returned mapping is an in-memory evaluation view. The original configuration,
GLM prompts, request transforms, source frames and production binding stay intact.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .io import digest
from .frozen_compat import (
    BASE_CONFIG,
    BASE_SHA256,
    ROOT,
    FrozenCompatibilityError,
    _path,
    _signed,
    sha,
    validate_companion,
)

REVISION_PATH='configs/reference_revision.json'
SITE_COMMIT='b5e77f9'
SITE_SHA256='78a4775b7abcfe225dfb91f73829fcfaf98adbe0afac8e9bc376e4c9a61701a1'
ARTIFACT_ROLES={'instrument','oracle_prompt','site_instrument','site_guidelines','pair_schema',
    'extraction_schema','pair_runtime','instrument_runtime','comparison_runtime','comparison_prompt','annotation_names_runtime'}
EVALUATION_HASH_ROLES={'instrument_sha256':'instrument','oracle_prompt_sha256':'oracle_prompt',
    'extraction_comparison_prompt_sha256':'comparison_prompt','site_instrument_sha256':'site_instrument'}


def reference_view(base,document,*,revision_sha256,root=ROOT):
    """Pure mapping transformation after the caller validates the signed companion."""
    artifacts=document.get('artifact_paths',{})
    if set(artifacts)!=ARTIFACT_ROLES or document.get('site_commit')!=SITE_COMMIT:
        raise FrozenCompatibilityError('Reference revision has an incomplete or obsolete artifact map')
    additional=document.get('new_files',{})
    for role,path in artifacts.items():
        if path not in additional or additional[path]!=sha(_path(root,path)):
            raise FrozenCompatibilityError('Reference artifact is not an exact signed additional input: '+role)
    if additional[artifacts['site_instrument']]!=SITE_SHA256:
        raise FrozenCompatibilityError('Reference revision is not the final authorized site export')
    effective=deepcopy(base)
    effective['reference_paths']=deepcopy(artifacts)
    for field,role in EVALUATION_HASH_ROLES.items():
        effective['evaluation'][field]=additional[artifacts[role]]
    runtime=deepcopy(base.get('reference_runtime_sha256',{}))
    runtime.update({path:entry['approved_sha256'] for path,entry in document['replacements'].items()})
    runtime.update(additional)
    effective['reference_runtime_sha256']=runtime
    identity={'base_evaluation_config_sha256':BASE_SHA256,'reference_revision_sha256':revision_sha256,
        'site_commit':SITE_COMMIT,'site_instrument_sha256':SITE_SHA256,
        'evaluation_hashes':{field:effective['evaluation'][field] for field in EVALUATION_HASH_ROLES},
        'reference_runtime_sha256':runtime}
    effective['_reference_revision']={
        'base_evaluation_config_sha256':BASE_SHA256,'reference_revision_sha256':revision_sha256,
        'reference_effective_sha256':digest(identity),'site_commit':SITE_COMMIT,
        'site_instrument_sha256':SITE_SHA256}
    return effective


def effective_reference_config(config_path=BASE_CONFIG,*,root=ROOT):
    """Require the approved latest reference revision; never fall back to old words."""
    root=Path(root).resolve();config_path=_path(root,config_path);path=_path(root,REVISION_PATH)
    if sha(config_path)!=BASE_SHA256:raise FrozenCompatibilityError('Reference base config changed')
    if not path.exists():raise FrozenCompatibilityError('Latest reference revision has not been registered')
    _signed(config_path,root);_signed(path,root)
    base=json.loads(config_path.read_text());document=json.loads(path.read_text())
    validate_companion(document,'reference',root=root,config_path=config_path)
    return reference_view(base,document,revision_sha256=sha(path),root=root)


def reference_provenance(config):
    value=config.get('_reference_revision')
    if not isinstance(value,dict) or value.get('base_evaluation_config_sha256')!=BASE_SHA256:
        raise FrozenCompatibilityError('Effective reference provenance is missing')
    return deepcopy(value)


def require_reference_provenance(record,config):
    expected=reference_provenance(config)
    if record.get('reference_revision')!=expected:
        raise FrozenCompatibilityError('Artifact does not bind the active reference revision')


def reference_revision_files(config,*,root=ROOT):
    """Additional immutable files to revalidate before each reference call."""
    provenance=reference_provenance(config);path=_path(root,REVISION_PATH)
    if sha(path)!=provenance['reference_revision_sha256']:
        raise FrozenCompatibilityError('Reference companion changed after plan construction')
    document=json.loads(path.read_text())
    files={str(path.resolve()):sha(path)}
    for relative in [document['protocol_path'],document['review_path'],*document.get('new_files',{})]:
        resolved=_path(root,relative);files[str(resolved)]=sha(resolved)
    return files
