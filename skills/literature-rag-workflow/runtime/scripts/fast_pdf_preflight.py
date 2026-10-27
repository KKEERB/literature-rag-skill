"""Local ARS preflight adapter: include the active, parsed XRef stream itself.

No source PDF or vendored ARS file is changed. All original tree-count, parser
warning, trailing-data and stale-revision gates run in the upstream checker.
Only an already parsed XRef stream at a unique active-chain offset is added to
the reader's in-memory direct-object index. Calibrated inputs additionally need
qpdf --check exit 0. Unknown/deleted objects are never generally exempted.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import types

UPSTREAM = Path(__file__).resolve().parents[1] / 'third_party/academic-research-skills/scripts/pdf_read_preflight.py'
UPSTREAM_SHA256 = 'e4a2431fe2f2525ec755877869a93ee4ff2dd13a44f294d38ceb2e0736b5af16'
TOOL_VERSION = 'fast_pdf_preflight/1.1.0'


def upstream_module():
    if hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        raise ValueError('fixed upstream preflight changed; review adapter compatibility first')
    sys.path.insert(0, str(UPSTREAM.parent))
    spec = importlib.util.spec_from_file_location('_fast_pdf_upstream_preflight', UPSTREAM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_preflight(path):
    calibrated = []
    try:
        upstream = upstream_module()
        real_pypdf = upstream.pypdf
        if real_pypdf is not None:
            def reader_factory(stream):
                reader = real_pypdf.PdfReader(stream)
                data = stream.getvalue()
                # These are captured before any page/root traversal; pypdf has
                # resolved the active xref chain, not arbitrary document objects.
                streams = {key: obj for key, obj in reader.resolved_objects.items()
                           if hasattr(obj, 'get') and str(obj.get('/Type', '')) == '/XRef'}
                startxrefs = list(re.finditer(rb'startxref\s+(\d+)', data))
                targets = {int(startxrefs[-1].group(1))} if startxrefs else set()
                for obj in streams.values():
                    for name in ('/Prev', '/XRefStm'):
                        value = obj.get(name)
                        if isinstance(value, int):
                            targets.add(value)
                hybrid = reader.trailer.get('/XRefStm')
                if isinstance(hybrid, int):
                    targets.add(hybrid)
                for (generation, number), obj in streams.items():
                    # Refuse duplicate raw definitions, including an appended
                    # unreferenced replacement with a stale startxref.
                    pattern = (rb'(?<![0-9])' + str(number).encode() + rb'[\x00\t\n\x0c\r ]+'
                               + str(generation).encode() + rb'[\x00\t\n\x0c\r ]+obj\b')
                    headers = list(re.finditer(pattern, data))
                    if len(headers) != 1 or headers[0].start() not in targets:
                        continue
                    if any(number in table for table in reader.xref.values() if isinstance(table, dict)):
                        continue
                    offset = headers[0].start()
                    reader.xref.setdefault(generation, {})[number] = offset
                    calibrated.append({'object': number, 'generation': generation, 'offset': offset})
                return reader
            upstream.pypdf = types.SimpleNamespace(PdfReader=reader_factory)
        result = upstream.run_preflight(path)
        result['upstream_tool'] = result['tool']
        result['upstream_sha256'] = UPSTREAM_SHA256
        result['tool'] = TOOL_VERSION
        result['active_xref_streams_calibrated'] = calibrated
        if calibrated:
            try:
                checked = subprocess.run(['qpdf', '--check', str(path)], capture_output=True, timeout=120)
                result['independent_qpdf_check'] = {'exit_code': checked.returncode,
                    'stdout': checked.stdout.decode(errors='replace'), 'stderr': checked.stderr.decode(errors='replace')}
                if checked.returncode != 0:
                    result['warnings'].append('independent-qpdf: calibrated input did not pass without warnings')
                    if result['verdict'] == 'PASS':
                        result['verdict'] = 'UNAVAILABLE'
            except (OSError, subprocess.SubprocessError) as exc:
                result['warnings'].append('independent-qpdf-unavailable: ' + str(exc))
                if result['verdict'] == 'PASS':
                    result['verdict'] = 'UNAVAILABLE'
        return result
    except (OSError, ValueError, ImportError) as exc:
        return {'schema': 'pdf_read_preflight/1', 'verdict': 'UNAVAILABLE', 'tool': TOOL_VERSION,
                'file': str(path), 'warnings': ['adapter-unavailable: ' + str(exc)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf')
    args = parser.parse_args()
    print(json.dumps(run_preflight(args.pdf), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
