"""Explicit signed runtime/reference exceptions to the immutable v2 freeze.

No automatic rebinding. Only named companion files, original archived bytes,
reviewed replacement hashes and protected request-builder equality may authorize
an exception. This module never changes configuration or production identities.
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
BASE_CONFIG='configs/evaluation_system.json'
BASE_SHA256='d8f7d94abc966626d5153bcb6307678ee34541c942787aeb904380a939c1d80a'
COMPANIONS={'operational':'configs/operational_revision.json','reference':'configs/reference_revision.json'}
ALLOWED={
 'operational':{'src/chl_rag/spend_budget.py','src/chl_rag/model_client.py','src/chl_rag/model_runs.py','src/chl_rag/production.py'},
 'reference':{'src/chl_rag/reference_oracles.py','src/chl_rag/v2_evaluate.py','src/chl_rag/sampling.py',
    'src/chl_rag/instrument_original.py','src/chl_rag/pair_instrument_original.py','src/chl_rag/extraction_comparison_original.py',
    'configs/instrument_original.json','configs/oracle_prompt_original.json','configs/pair_annotation_schema_original.json',
    'configs/extraction_annotation_schema_original.json','configs/extraction_comparison_prompt_original.json',
    'configs/site_instrument_first_site.json','configs/site_guidelines_en_first_site.md'},
}
PROTECTED={
 'src/chl_rag/model_client.py':('V2Client.make_request','V2Client.route','V2Client.reserve_cost'),
 'src/chl_rag/model_runs.py':('_signed_file','configuration_descriptor','scientific_inputs','_parse_response'),
}


class FrozenCompatibilityError(RuntimeError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _path(root,relative):
    root=Path(root).resolve();path=(root/relative).resolve()
    if not path.is_relative_to(root):raise FrozenCompatibilityError('Revision path leaves repository')
    return path


def _signed(path,root):
    relative=Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    actual=Path(path).read_bytes()
    committed=subprocess.run(['git','show','HEAD:'+relative],cwd=root,capture_output=True,check=False)
    if committed.returncode or committed.stdout!=actual:
        raise FrozenCompatibilityError('Revision input differs from signed HEAD: '+relative)
    revision=subprocess.run(['git','log','-1','--format=%H','--',relative],cwd=root,capture_output=True,text=True,check=True).stdout.strip()
    if not revision or subprocess.run(['git','verify-commit',revision],cwd=root,capture_output=True,check=False).returncode:
        raise FrozenCompatibilityError('Revision input has no verified signature: '+relative)


def _node(source,name):
    node=ast.parse(source)
    for part in name.split('.'):
        matches=[value for value in node.body if isinstance(value,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and value.name==part]
        if len(matches)!=1:raise FrozenCompatibilityError('Protected function missing or ambiguous: '+name)
        node=matches[0]
    return ast.dump(node,include_attributes=False)


def preserved_producer_code(path,old,new):
    """Pure AST checks; no model/source data is involved."""
    if path=='src/chl_rag/production.py':
        def constrained_tree(text):
            tree=ast.parse(text)
            tree.body=[node for node in tree.body if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) or node.name!='production_guard']
            for node in tree.body:
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name=='_classify_one':
                    for branch in ast.walk(node):
                        if isinstance(branch,ast.ExceptHandler):
                            for operation in ast.walk(branch):
                                targets=(operation.targets if isinstance(operation,ast.Assign) else
                                    [operation.target] if isinstance(operation,(ast.AnnAssign,ast.AugAssign)) else [])
                                for target in targets:
                                    names={n.id for n in ast.walk(target) if isinstance(n,ast.Name)}
                                    if names & {'spec','request','config','pair','articles','extractions','verdict'}:
                                        raise FrozenCompatibilityError('Transport handler changes scientific inputs or invents a verdict')
                            branch.body=[]
            return ast.dump(tree,include_attributes=False)
        if constrained_tree(old)!=constrained_tree(new):
            raise FrozenCompatibilityError('Only production_guard and classification exception-handler bodies may change')
    for name in PROTECTED.get(path,()):
        if _node(old,name)!=_node(new,name):
            raise FrozenCompatibilityError('Frozen request transformation changed: '+name)


def validate_companion(document,kind,*,root=ROOT,config_path=BASE_CONFIG,verify_signature=_signed):
    """Validate all entries together, never a convenient per-file partial subset."""
    root=Path(root).resolve();config_path=_path(root,config_path)
    if (kind not in COMPANIONS or document.get('schema_version')!=1 or document.get('status')!='registered'
            or document.get('kind')!=kind or document.get('base_config_path')!=BASE_CONFIG
            or document.get('base_config_sha256')!=BASE_SHA256 or sha(config_path)!=BASE_SHA256):
        raise FrozenCompatibilityError('Revision does not bind the immutable base configuration')
    if document.get('production_binding_changed') is not False or document.get('glm_prompts_changed') is not False:
        raise FrozenCompatibilityError('Revision cannot change production identity or GLM prompts')
    helper=_path(root,'src/chl_rag/frozen_compat.py')
    if document.get('compatibility_module_sha256')!=sha(helper):
        raise FrozenCompatibilityError('Compatibility verifier differs from reviewed bytes')
    verify_signature(helper,root)
    protocol=_path(root,document['protocol_path'])
    if sha(protocol)!=document.get('protocol_sha256'):raise FrozenCompatibilityError('Revision authority hash mismatch')
    verify_signature(protocol,root)
    review=_path(root,document['review_path'])
    if sha(review)!=document.get('review_sha256'):raise FrozenCompatibilityError('Revision review hash mismatch')
    verify_signature(review,root)
    base=json.loads(config_path.read_text());declared=base.get('source_sha256',{})
    entries=document.get('replacements')
    if not isinstance(entries,dict) or not entries or not set(entries)<=ALLOWED[kind]:
        raise FrozenCompatibilityError('Revision contains an unauthorized replacement path')
    for relative,binding in entries.items():
        original=declared.get(relative)
        if original is None or original!=binding.get('original_sha256'):
            raise FrozenCompatibilityError('Original hash is not in the production freeze: '+relative)
        archive=_path(root,binding['archive_path']);current=_path(root,relative)
        if sha(archive)!=original or sha(current)!=binding.get('approved_sha256'):
            raise FrozenCompatibilityError('Original archive or approved replacement changed: '+relative)
        verify_signature(archive,root);verify_signature(current,root)
        if relative.endswith('.py'):preserved_producer_code(relative,archive.read_text(),current.read_text())
    for relative,expected in document.get('new_files',{}).items():
        path=_path(root,relative)
        if relative in declared or sha(path)!=expected:raise FrozenCompatibilityError('New revision artifact changed or shadows frozen input')
        verify_signature(path,root)
    return document


def verify_frozen_input(path,expected_sha,*,config_path=BASE_CONFIG,root=ROOT):
    """Accept original bytes, or one complete explicit signed exception only."""
    root=Path(root).resolve();current=_path(root,path)
    if sha(current)==expected_sha:return
    relative=current.relative_to(root).as_posix();matches=[]
    for kind,companion in COMPANIONS.items():
        if relative not in ALLOWED[kind]:continue
        location=_path(root,companion)
        if not location.exists():continue
        document=json.loads(location.read_text())
        if relative not in document.get('replacements',{}):continue
        _signed(location,root)
        validated=validate_companion(document,kind,root=root,config_path=config_path)
        entry=validated['replacements'][relative]
        if entry['original_sha256']==expected_sha:matches.append(kind)
    if len(matches)!=1:raise FrozenCompatibilityError('Frozen input changed without one applicable signed exception: '+relative)
