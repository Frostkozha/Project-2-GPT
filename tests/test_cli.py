import json
from types import SimpleNamespace

import pytest

from pdf_ingest.cli import _exclusive_write, main, parser


def test_version(capsys):
    with pytest.raises(SystemExit) as result:
        main(['--version'])
    assert result.value.code == 0
    assert '0.1.0' in capsys.readouterr().out


def test_page_range_refuses_reversed():
    with pytest.raises(SystemExit) as result:
        parser().parse_args(['convert', 'input.pdf', '--source', 'source.json',
                             '--import-root', '.', '--output-root', 'out', '--page-range', '20:12'])
    assert result.value.code == 2


def test_bad_source_does_not_echo_input(tmp_path, capsys):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps({'source_id': 'secret-patient-name', 'secret': 'do-not-print'}))
    code = main(['convert', 'missing.pdf', '--source', str(source), '--import-root', '.', '--output-root', 'out'])
    assert code == 1
    captured = capsys.readouterr()
    assert 'secret-patient-name' not in captured.err
    assert 'do-not-print' not in captured.err
    assert 'INVALID_INPUT' in captured.err


def test_export_write_does_not_overwrite_or_follow_symlink(tmp_path):
    original = tmp_path / 'original'
    original.write_text('keep')
    symlink = tmp_path / 'symlink'
    symlink.symlink_to(original)
    with pytest.raises(FileExistsError):
        _exclusive_write(symlink, 'replacement')
    assert original.read_text() == 'keep'


def test_batch_reports_failure_and_continues(tmp_path, monkeypatch, capsys):
    import pdf_ingest.cli
    source = {'source_id': 'fixture', 'source_version': '1', 'title': 'Fixture',
              'assignment': 'developer', 'rights_reference': '', 'synthetic': True}
    registration = tmp_path / 'batch.json'
    registration.write_text(json.dumps([{'pdf': 'bad.pdf', 'source': {}}, {'pdf': 'good.pdf', 'source': source}]))
    calls = []
    def convert(args, pdf, source, span=None):
        calls.append(str(pdf))
        return SimpleNamespace(status='completed')
    monkeypatch.setattr(pdf_ingest.cli, '_convert', convert)
    # Make the fake result JSON-compatible while retaining the outcome attribute.
    monkeypatch.setattr(pdf_ingest.cli, '_json', lambda value: str(value))
    assert main(['batch', str(registration), '--import-root', '.', '--output-root', 'out']) == 1
    assert calls == ['good.pdf']
    assert 'blocked' in capsys.readouterr().out
