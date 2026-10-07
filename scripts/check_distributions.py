"""Reject untracked files in release archives, especially local capture reports."""
import argparse
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import zipfile


def check(directory):
    tracked = set(subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0'))
    archives = [p for p in Path(directory).iterdir() if p.name.endswith(('.whl', '.tar.gz', '.zip'))]
    if not archives:
        raise RuntimeError('No distributions found')
    for path in archives:
        wheel = path.suffix == '.whl'
        if path.name.endswith('.tar.gz'):
            with tarfile.open(path) as archive:
                names = [member.name for member in archive if member.isfile()]
        else:
            with zipfile.ZipFile(path) as archive:
                names = [info.filename for info in archive.infolist() if not info.is_dir()]
        for name in names:
            parts = PurePosixPath(name).parts
            relative = '/'.join(parts if wheel else parts[1:])
            if any(part in ('data', 'reports', '__pycache__', '.git', '.venv') for part in parts):
                raise RuntimeError(f'{path.name}: private/runtime path {name}')
            generated = (wheel and '.dist-info/' in name) or '.egg-info/' in name or relative in ('PKG-INFO', 'setup.cfg')
            if not generated and relative not in tracked:
                raise RuntimeError(f'{path.name}: untracked release payload {relative}')
        print(f'{path.name}: {len(names)} files checked against Git')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', nargs='?', default='dist')
    check(parser.parse_args().directory)
