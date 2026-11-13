"""Build deterministic, credential-free CLI/plugin ZIPs from the committed tree.
No network or publication. Output defaults to dist/ (gitignored).
"""
import argparse
import hashlib
import json
import subprocess
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'dist')
    args = parser.parse_args()
    if git('status', '--porcelain'):
        raise SystemExit('Commit changes before building release packages.')
    version = json.loads((ROOT/'plugin.json').read_text(encoding='utf-8'))['version']
    commit = git('rev-parse', 'HEAD').decode().strip()
    timestamp = time.gmtime(int(git('show', '-s', '--format=%ct', 'HEAD')))
    files = git('ls-files', '-z').decode('utf-8').rstrip('\0').split('\0')
    core = [n for n in files if n.startswith('skills/remote-naraeon/')]
    shared = [n for n in ['plugin.json', 'README.md', 'LICENSE', '사용안내.md', '여러 태블릿 사용안내.md'] if n in files]
    cli = core + shared + [n for n in files if '/' not in n and n.endswith('.cmd')]
    plugin = core + shared
    args.output.mkdir(parents=True, exist_ok=True)
    result = {'version': version, 'commit': commit, 'packages': []}
    for kind, names in [('cli', cli), ('plugin', plugin)]:
        path = args.output/f'remote-naraeon-{kind}-{version}.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(set(names)):
                if Path(name).suffix in {'.dpapi', '.vault', '.session', '.key', '.pem'} or '__pycache__' in name:
                    raise SystemExit('Private/generated file cannot be packaged: ' + name)
                data = git('show', 'HEAD:' + name)  # Commit bytes, not CRLF working-tree transformations.
                info = zipfile.ZipInfo(name, date_time=timestamp[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_name(path.name+'.sha256.txt').write_text(f'{digest}  {path.name}\n', encoding='ascii')
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            assert 'skills/remote-naraeon/web/app.js' in archive.namelist()
            assert json.loads(archive.read('plugin.json'))['version'] == version
        result['packages'].append({'file': str(path.resolve()), 'sha256': digest, 'bytes': path.stat().st_size})
    receipt = args.output/'build-receipt.json'
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
