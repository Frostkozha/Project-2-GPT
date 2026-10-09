"""Generate an author-created PDF and hash-bound, unapproved source register.

Run from the repository with `.venv/bin/python scripts/make_demo.py imports`.
Requires the development dependency group (ReportLab). No approvals are granted.
"""
import hashlib
import json
import sys
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas


def main() -> None:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else 'imports')
    destination.mkdir(parents=True, exist_ok=True)
    pdf = destination / 'demo.pdf'
    register = destination / 'demo-source.json'
    if pdf.exists() or register.exists():
        raise SystemExit('Demo files already exist; choose another directory.')
    with pdf.open('xb') as stream:
        canvas = Canvas(stream)
        canvas.setFont('Helvetica-Bold', 20)
        canvas.drawString(72, 760, 'Synthetic course notes')
        canvas.setFont('Helvetica', 12)
        canvas.drawString(72, 710, 'Value: -0.5 mg. No change is expected in this fixture.')
        canvas.showPage()
        canvas.setFont('Helvetica', 12)
        canvas.drawString(72, 720, 'Second physical page; original page indices are required.')
        canvas.save()
    source = {'tenant_id': 'local', 'source_id': 'demo-course', 'source_version': '1',
              'title': 'Author-created synthetic course notes', 'assignment': 'developer-fixtures',
              'rights_reference': 'synthetic-author-created', 'rights_confirmed': True,
              'content_approved': False, 'content_approval_reference': None, 'eligible': True,
              'synthetic': True, 'expected_source_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest()}
    with register.open('x', encoding='utf-8') as stream:
        json.dump(source, stream, indent=2)
        stream.write('\n')
    print(f'Created {pdf} and {register}; content remains unapproved.')


if __name__ == '__main__':
    main()
