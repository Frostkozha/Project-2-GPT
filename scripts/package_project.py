"""Create a ZIP of committed source, excluding local data and dependencies."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        parser.error('Output exists; choose a new archive filename.')
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        archive = Path(temporary) / 'source.zip'
        subprocess.run(['git', 'archive', '--format=zip', '--prefix=Project-2-GPT/',
                        f'--output={archive}', 'HEAD'], cwd=root, check=True)
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise RuntimeError('Archive integrity check failed.')
            required = {'Project-2-GPT/README.md', 'Project-2-GPT/uv.lock',
                        'Project-2-GPT/pdf_ingest/cli.py'}
            if not required.issubset(bundle.namelist()):
                raise RuntimeError('Archive is missing required project files.')
        # Publish without replacing an existing file, including a symlink.
        os.link(archive, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f'Created {destination}\nSHA256 {digest}')


if __name__ == '__main__':
    main()
