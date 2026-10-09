"""Operator CLI. Every source approval is explicit; no command activates an index."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import __version__
from .schema import IngestError, Limits, SourceSpec


def available_memory() -> int:
    """Use current cgroup headroom and Linux available RAM, never total host RAM alone."""
    budgets = []
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                budgets.append(int(line.split()[1]) * 1024)
        maximum = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if maximum != 'max':
            current = int(Path('/sys/fs/cgroup/memory.current').read_text().strip())
            budgets.append(max(0, int(maximum) - current))
    except (OSError, ValueError):
        pass
    if not budgets:
        raise IngestError('RESOURCE_LIMIT', 'Cannot measure available RAM; supply --memory-mib explicitly.')
    return min(budgets)


def limits_from_args(args: argparse.Namespace) -> Limits:
    memory = args.memory_mib * 1024**2 if args.memory_mib else min(8 * 1024**3, available_memory() // 2)
    if memory < 1024**3:
        raise IngestError('RESOURCE_LIMIT', 'Insufficient available memory for the layout worker.')
    return Limits(memory_bytes=memory, scratch_bytes=args.scratch_mib * 1024**2,
                  job_timeout_seconds=args.job_timeout, page_timeout_seconds=args.page_timeout,
                  max_range_pages=args.max_range_pages, worker_threads=args.threads)


def load_source(path: Path) -> SourceSpec:
    return SourceSpec.model_validate_json(path.read_bytes())


def _json(value: Any) -> str:
    def default(item):
        if hasattr(item, 'model_dump'):
            return item.model_dump(mode='json')
        if isinstance(item, Path):
            return str(item)
        if hasattr(item, '__dataclass_fields__'):
            import dataclasses
            return dataclasses.asdict(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, ensure_ascii=False, indent=2, default=default, allow_nan=False)


def _pages(value: str) -> tuple[int, int]:
    try:
        start, end = (int(part) for part in value.split(':'))
        if start < 1 or end < start:
            raise ValueError
        return start, end
    except ValueError as exc:
        raise argparse.ArgumentTypeError('use an inclusive 1-based range START:END') from exc


def _page_list(value: str) -> list[int]:
    try:
        pages = [int(x) for x in value.split(',')]
        if any(x < 1 for x in pages) or len(set(pages)) != len(pages):
            raise ValueError
        return pages
    except ValueError as exc:
        raise argparse.ArgumentTypeError('use unique positive page indices separated by commas') from exc


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='pdf-ingest', description=__doc__)
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='command', required=True)
    convert = commands.add_parser('convert', help='convert a registered local PDF in an isolated worker')
    convert.add_argument('pdf', type=Path)
    convert.add_argument('--source', required=True, type=Path, help='current SourceSpec JSON, never credentials')
    _conversion_options(convert)
    batch = commands.add_parser('batch', help='convert independent registrations; emit every outcome')
    batch.add_argument('registrations', type=Path, help='JSON array of {pdf, source, page_range?}; source is an object')
    _conversion_options(batch)
    for name in ('validate', 'review-status'):
        command = commands.add_parser(name)
        command.add_argument('bundle', type=Path)
    correct = commands.add_parser('correct', help='create a new unapproved conversion version')
    correct.add_argument('bundle', type=Path)
    correct.add_argument('--block-id', required=True)
    correct.add_argument('--text-file', required=True, type=Path, help='exact UTF-8 replacement source transcription')
    correct.add_argument('--reason', required=True)
    correct.add_argument('--reviewer', required=True)
    correct.add_argument('--output-root', required=True, type=Path)
    table = commands.add_parser('table-representation', help='record a reviewed structured-text table rendering')
    table.add_argument('bundle', type=Path)
    table.add_argument('--block-id', required=True)
    table.add_argument('--text-file', required=True, type=Path)
    table.add_argument('--reason', required=True)
    table.add_argument('--reviewer', required=True)
    table.add_argument('--output-root', required=True, type=Path)
    resolve = commands.add_parser('resolve', help='record source review of one issue; does not approve source')
    resolve.add_argument('bundle', type=Path)
    resolve.add_argument('--issue-id', required=True)
    resolve.add_argument('--resolution', choices=('approved', 'excluded'), required=True)
    resolve.add_argument('--reviewer', required=True)
    label = commands.add_parser('verify-label', help='record a printed page label verified against the PDF')
    label.add_argument('bundle', type=Path)
    label.add_argument('--page', required=True, type=int)
    label.add_argument('--label', required=True)
    label.add_argument('--reviewer', required=True)
    label.add_argument('--output-root', required=True, type=Path)
    reclassify = commands.add_parser('reclassify', help='review paragraph/furniture classification in a new version')
    reclassify.add_argument('bundle', type=Path)
    reclassify.add_argument('--block-id', required=True)
    reclassify.add_argument('--kind', choices=('paragraph', 'furniture'), required=True)
    reclassify.add_argument('--reason', required=True)
    reclassify.add_argument('--reviewer', required=True)
    reclassify.add_argument('--output-root', required=True, type=Path)
    approve = commands.add_parser('approve', help='explicit approval of canonical content and scope')
    approve.add_argument('bundle', type=Path)
    approve.add_argument('--source', required=True, type=Path)
    approve.add_argument('--reviewer', required=True)
    approve.add_argument('--method', choices=('direct', 'source_level_sampled'), default='direct')
    approve.add_argument('--block-ids', nargs='+')
    approve.add_argument('--sampled-pages', type=_page_list)
    approve.add_argument('--permitted-subset-reference')
    export = commands.add_parser('export', help='write reviewed canonical passages; never activate an index')
    export.add_argument('bundle', type=Path)
    export.add_argument('--source', required=True, type=Path, help='current trusted source registration; revocation checked')
    export.add_argument('--output', required=True, type=Path)
    jobs = commands.add_parser('jobs')
    jobs.add_argument('--output-root', required=True, type=Path)
    cancel = commands.add_parser('cancel')
    cancel.add_argument('job_id')
    cancel.add_argument('--output-root', required=True, type=Path)
    provision = commands.add_parser('provision-models', help='explicit network-enabled install, outside conversion')
    provision.add_argument('--models-dir', required=True, type=Path)
    provision.add_argument('--with-ocr', action='store_true', help='also pin locally installed Tesseract 5 English data')
    provision.add_argument('--tessdata-dir', type=Path)
    doctor = commands.add_parser('doctor', help='check platform, limits, dependencies and local model readiness')
    doctor.add_argument('--models-dir', type=Path, default=Path('.models'))
    doctor.add_argument('--profile', choices=('native_layout_v1', 'english_ocr_v1'), default='native_layout_v1')
    evaluate = commands.add_parser('evaluate', help='measure a supplied annotated corpus, never infer accuracy')
    evaluate.add_argument('annotations', type=Path)
    evaluate.add_argument('--output', type=Path)
    preview = commands.add_parser('preview', help='render a local PDF page in the bounded offline worker')
    preview.add_argument('pdf', type=Path)
    preview.add_argument('--source', required=True, type=Path)
    preview.add_argument('--import-root', required=True, type=Path)
    preview.add_argument('--page', type=int, required=True)
    preview.add_argument('--output', type=Path, required=True)
    preview.add_argument('--memory-mib', type=int)
    preview.add_argument('--scratch-mib', type=int, default=128)
    preview.add_argument('--job-timeout', type=float, default=120.)
    preview.add_argument('--page-timeout', type=float, default=120.)
    preview.add_argument('--max-range-pages', type=int, default=1)
    preview.add_argument('--threads', type=int, default=1)
    return root


def _conversion_options(command: argparse.ArgumentParser) -> None:
    command.add_argument('--import-root', type=Path, required=True)
    command.add_argument('--output-root', type=Path, required=True)
    command.add_argument('--models-dir', type=Path, default=Path('.models'))
    command.add_argument('--profile', choices=('native_layout_v1', 'english_ocr_v1'), default='native_layout_v1')
    command.add_argument('--page-range', type=_pages)
    command.add_argument('--memory-mib', type=int, help='enforced address-space cap, defaults to measured RAM budget')
    command.add_argument('--scratch-mib', type=int, default=1024)
    command.add_argument('--job-timeout', type=float, default=1800)
    command.add_argument('--page-timeout', type=float, default=120)
    command.add_argument('--max-range-pages', type=int, default=2000)
    command.add_argument('--threads', type=int, default=2)
    command.add_argument('--force', action='store_true', help='new conversion record rather than extraction cache reuse')


def _convert(args, pdf, source, page_range=None):
    if sys.platform != 'linux':
        raise IngestError('RESOURCE_LIMIT', 'The conversion worker requires the tested Linux syscall sandbox.')
    from .jobs import convert
    return convert(pdf_path=pdf, import_root=args.import_root, output_root=args.output_root,
                   source=source, profile=args.profile, models_dir=args.models_dir,
                   limits=limits_from_args(args), page_range=page_range or args.page_range, force=args.force)


def _status(result) -> str:
    return result.status if hasattr(result, 'status') else result['status']


def _exclusive_write(path: Path, text: str) -> None:
    """Never replace an existing operator output or follow a symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)


def run(args: argparse.Namespace) -> tuple[Any, int]:
    if args.command == 'convert':
        result = _convert(args, args.pdf, load_source(args.source))
        return result, 0 if _status(result) == 'completed' else 1
    if args.command == 'batch':
        rows = json.loads(args.registrations.read_text(encoding='utf-8'))
        if not isinstance(rows, list) or len(rows) > 1000:
            raise IngestError('INVALID_INPUT', 'Batch must be a JSON array of at most 1000 registrations.')
        results = []
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict) or set(row) - {'pdf', 'source', 'page_range'}:
                    raise ValueError('Invalid registration fields.')
                source = SourceSpec.model_validate(row['source'])
                span = _pages(row['page_range']) if row.get('page_range') else None
                result = _convert(args, Path(row['pdf']), source, span)
                results.append({'registration_index': index, 'result': result, 'status': _status(result)})
            except (KeyError, ValueError, OSError, argparse.ArgumentTypeError) as exc:
                results.append({'registration_index': index, 'status': 'blocked',
                                'code': getattr(exc, 'code', 'INVALID_INPUT'),
                                'message': getattr(exc, 'message', 'Invalid registration or unreadable local input.')})
        return results, 0 if all(r['status'] == 'completed' for r in results) else 1
    if args.command == 'validate':
        from .serialize import validate_bundle
        manifest, blocks, report = validate_bundle(args.bundle)
        return {'valid': True, 'conversion_id': manifest.conversion_id, 'pages': len(manifest.page_inventory),
                'blocks': len(blocks), 'issues': len(report.issues), 'review_state': manifest.review_state}, 0
    if args.command == 'review-status':
        from .review import review_status
        return review_status(args.bundle), 0
    if args.command in ('correct', 'table-representation'):
        from .review import approve_table_representation, correct_bundle
        text = args.text_file.read_bytes().decode('utf-8')
        if args.command == 'correct':
            result = correct_bundle(args.bundle, args.block_id, text, args.reason, args.reviewer, args.output_root)
        else:
            result = approve_table_representation(args.bundle, args.block_id, text, args.reason, args.reviewer, args.output_root)
        return {'bundle_path': result, 'review_state': 'needs_review'}, 0
    if args.command == 'resolve':
        from .review import resolve_issue
        resolve_issue(args.bundle, args.issue_id, args.resolution, args.reviewer)
        return {'issue_id': args.issue_id, 'resolution': args.resolution}, 0
    if args.command == 'verify-label':
        from .review import verify_page_label
        result = verify_page_label(args.bundle, args.page, args.label, args.reviewer, args.output_root)
        return {'bundle_path': result, 'review_state': 'needs_review'}, 0
    if args.command == 'reclassify':
        from .review import reclassify_block
        result = reclassify_block(args.bundle, args.block_id, args.kind, args.reason, args.reviewer, args.output_root)
        return {'bundle_path': result, 'review_state': 'needs_review'}, 0
    if args.command == 'approve':
        from .review import approve_bundle
        approve_bundle(args.bundle, load_source(args.source), args.reviewer, args.method,
                       args.block_ids, args.sampled_pages, args.permitted_subset_reference)
        return {'review_state': 'approved'}, 0
    if args.command == 'export':
        from .import_adapter import iter_passages
        # Materialize and recheck all passages before writing any evidence.
        passages = list(iter_passages(args.bundle, load_source(args.source)))
        _exclusive_write(args.output, ''.join(json.dumps(p.model_dump(mode='json') if hasattr(p, 'model_dump') else p,
                                                      ensure_ascii=False, allow_nan=False) + '\n' for p in passages))
        return {'passages': len(passages), 'output': args.output, 'index_activated': False}, 0
    if args.command in ('jobs', 'cancel'):
        from .jobs import cancel_job, list_jobs
        if args.command == 'jobs':
            return list_jobs(args.output_root), 0
        result = cancel_job(args.output_root, args.job_id)
        return result, 0 if _status(result) == 'cancelled' else 1
    if args.command == 'provision-models':
        from .provisioning import provision_models
        return provision_models(args.models_dir, with_ocr=args.with_ocr, tessdata_dir=args.tessdata_dir), 0
    if args.command == 'doctor':
        import importlib.metadata
        import ctypes.util
        from .provisioning import validate_models
        status = {'version': __version__, 'platform': sys.platform, 'linux_required': True,
                  'seccomp_library': ctypes.util.find_library('seccomp'), 'dependencies': {}}
        try:
            status['available_memory_bytes'] = available_memory()
        except IngestError as exc:
            status['memory_error'] = exc.code
        for package in ('docling', 'pypdf', 'pydantic', 'torch', 'transformers'):
            try:
                status['dependencies'][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                status['dependencies'][package] = None
        try:
            status['models'] = validate_models(args.models_dir, args.profile)
        except IngestError as exc:
            status['models_error'] = {'code': exc.code, 'message': exc.message}
        ok = sys.platform == 'linux' and status['seccomp_library'] and 'models_error' not in status and 'memory_error' not in status
        status['ready'] = bool(ok)
        return status, 0 if ok else 1
    if args.command == 'evaluate':
        from .evaluation import evaluate_corpus
        result = evaluate_corpus(args.annotations)
        if args.output:
            _exclusive_write(args.output, _json(result) + '\n')
        return result, 0
    if args.command == 'preview':
        if sys.platform != 'linux':
            raise IngestError('RESOURCE_LIMIT', 'The preview worker requires the tested Linux syscall sandbox.')
        from .jobs import render_preview
        result = render_preview(args.pdf, args.import_root, load_source(args.source), limits_from_args(args), args.page, args.output)
        return {'preview': result, 'pdf_page_index': args.page}, 0
    raise IngestError('INVALID_INPUT', 'Unsupported command.')


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result, code = run(args)
        print(_json(result))
        return code
    except IngestError as exc:
        print(_json({'code': exc.code, 'message': exc.message}), file=sys.stderr)
        return 1
    except ValidationError:
        # Pydantic errors include input_value, which can contain source text/private paths.
        print(_json({'code': 'INVALID_INPUT', 'message': 'Input does not satisfy the required schema.'}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print(_json({'code': 'INVALID_INPUT', 'message': 'Unreadable input, invalid data, or output already exists.'}), file=sys.stderr)
        return 1
