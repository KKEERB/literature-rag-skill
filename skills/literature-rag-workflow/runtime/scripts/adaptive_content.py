"""Explicit per-model coverage; future scoped defaults come from the intake policy."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

POLICY = 'adaptive_coverage_v1'
MODULES = ('navigation', 'identity', 'core_work', 'background', 'physical_mechanism',
           'first_principles', 'method_audit', 'figures', 'contribution_audit',
           'critical_audit', 'self_test', 'research_transfer', 'group_meeting', 'appendices')


def adaptive(model):
    return model.get('generation', {}).get('content_policy') == POLICY


def validate_coverage(model):
    if not adaptive(model):
        return []
    coverage = model.get('generation', {}).get('coverage')
    if not isinstance(coverage, dict):
        return ['adaptive policy requires explicit original-source coverage']
    errors = []
    modules = coverage.get('modules', {})
    if set(modules) != set(MODULES):
        errors.append('coverage.modules must contain the fourteen module names')
    for name, item in modules.items():
        if not isinstance(item, dict) or type(item.get('applicable')) is not bool:
            errors.append('module requires boolean applicability: ' + name)
        elif not item['applicable'] and not str(item.get('reason_zh', '')).strip():
            errors.append('folded module requires a specific reason: ' + name)
    for required in ('identity', 'core_work', 'self_test'):
        if modules.get(required, {}).get('applicable') is not True:
            errors.append('essential module cannot be folded: ' + required)
    claims = {item['id'] for item in model.get('core_claims', [])}
    covered = set()
    work = coverage.get('core_work', [])
    if not work:
        errors.append('source core work inventory must not be empty')
    for item in work:
        ids = set(item.get('claim_ids', []))
        if not ids or not ids <= claims or not item.get('source_locator') or not item.get('description_zh'):
            errors.append('core work coverage needs valid claims, description and original locator')
        covered |= ids
    if covered != claims:
        errors.append('all core claims must belong to original-source core work coverage')
    figure_ids = {item['id'] for item in model.get('figures', [])}
    covered_figures = set()
    inventory = coverage.get('substantive_figures', [])
    for entry in inventory:
        if entry.get('disposition') == 'reviewed' and entry.get('figure_id') in figure_ids:
            covered_figures.add(entry['figure_id'])
        elif entry.get('disposition') != 'not_applicable' or not entry.get('reason_zh'):
            errors.append('every substantive figure needs a review or justified disposition')
    if covered_figures != figure_ids:
        errors.append('substantive figure inventory and actual figure reviews differ')
    if not inventory and not coverage.get('no_substantive_figures_reason_zh'):
        errors.append('empty figure inventory needs an original-source reason')
    capabilities = coverage.get('learning_capabilities', {})
    question_ids = {item['id'] for item in model.get('self_test', [])}
    for name in ('fact_evidence', 'mechanism_method', 'critique', 'transfer'):
        item = capabilities.get(name, {})
        ids = set(item.get('question_ids', []))
        if not (ids and ids <= question_ids) and not item.get('not_applicable_reason_zh'):
            errors.append('learning capability missing supported questions or reason: ' + name)
    if not question_ids:
        errors.append('adaptive self-test must still provide substantive learning questions')
    return errors


def folded_fields(model):
    if not adaptive(model):
        return set()
    modules = model.get('generation', {}).get('coverage', {}).get('modules', {})
    return {name for name, item in modules.items() if isinstance(item, dict)
            and item.get('applicable') is False and item.get('reason_zh')}


def fold_reader_markdown(markdown, model):
    """Keep fourteen headings in place and replace only explicit N/A sections."""
    import re
    folded = folded_fields(model)
    if not folded:
        return markdown
    sections = re.split(r'(?=^## \d+\.)', markdown, flags=re.M)
    output = [sections[0]]
    modules = model['generation']['coverage']['modules']
    for index, section in enumerate(sections[1:]):
        name = MODULES[index] if index < len(MODULES) else None
        if name in folded:
            heading = section.splitlines()[0]
            # Unnumbered answer/review sections can follow module 13. They are
            # independent deliverables and must survive an N/A appendix.
            tail = re.search(r'^## (?!\d+\.)', section, flags=re.M)
            suffix = section[tail.start():] if tail else ''
            section = heading + '\n\n本篇不适用：' + modules[name]['reason_zh'] + '\n\n' + suffix
        output.append(section)
    return ''.join(output)


def cached_check(cache_dir: Path, check_id: str, inputs: dict[str, Path], validator: Path, run):
    """Reuse only identical bytes and checker code; no cached writer/concurrency gate."""
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    def payload_digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
    binding = {'check_id': check_id, 'inputs': {name: sha(path) for name, path in inputs.items()},
               'validator_sha256': sha(validator)}
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (key + '.json')
    if path.exists():
        try:
            stored = json.loads(path.read_text())
            if (stored.get('binding') == binding and 'result' in stored
                    and stored.get('result_sha256') == payload_digest(stored['result'])):
                return stored['result'], True
        except (ValueError, TypeError, OSError):
            pass
    result = run()
    # Changes during the check invalidate the result rather than certify stale input.
    if binding['inputs'] != {name: sha(path) for name, path in inputs.items()} or sha(validator) != binding['validator_sha256']:
        raise ValueError('check inputs changed while running')
    import os
    import tempfile
    payload = {'binding': binding, 'result': result, 'result_sha256': payload_digest(result)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=cache_dir, prefix='.check-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return result, False
