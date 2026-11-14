"""Create an allowlisted source/build archive without local accounts or credentials."""
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    '.gitignore', 'README.md', 'CONTRACT.md', 'requirements.txt', 'requirements.lock.txt',
    'requirements-dev.txt', 'requirements-telegram.txt', 'start.ps1', 'start.cmd', 'Инструкция_ИИ_редактору.md',
    'Редакционные_правила_для_постов.md', 'Как_работать_с_ботом.md',
)


def release_files():
    paths = [ROOT / name for name in ROOT_FILES]
    for folder, suffixes in (
        ('server', {'.py'}), ('tests', {'.py'}), ('scripts', {'.py'}), ('docs', {'.md'}),
        ('web/src', {'.ts', '.tsx', '.css'}), ('web/public', {'.ttf'}),
        ('web/dist', {'.html', '.js', '.css', '.ttf'}),
    ):
        paths.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in suffixes)
    paths.extend(ROOT / 'web' / name for name in (
        '.gitignore', 'package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts', 'index.html',
    ))
    brand = ROOT / 'Основные и дополнительные визуальные элементы бренда'
    for colour, name in (('БЕЛЫЙ', 'ЛОГОТИП.png'), ('ЧЕРНЫЙ', 'ЛОГОТИП.png'), ('ОСНОВНОЙ ЦВЕТ', 'Логотип.png')):
        paths.append(brand / 'ЛОГОТИП' / colour / 'RUS' / '.PNG' / name)
    for name in ('avenirnextcyr-bold.ttf', 'avenirnextcyr-regular.ttf'):
        paths.append(brand / 'ШРИФТЫ' / 'ОСНОВНЫЕ' / 'AVENIR NEXT CYR' / name)
    paths = sorted(set(paths))
    if any(not path.is_file() for path in paths):
        raise RuntimeError('Missing required build file. Run npm run build in web first.')
    return paths


def verify(paths):
    # Compare against locally supplied keys without printing their values.
    secrets = set()
    pattern = re.compile(rb'(?:sk-[A-Za-z0-9_-]{16,}|[0-9]{7,}:[A-Za-z0-9_-]{25,})')
    for keyfile in ROOT.glob('*API*.txt'):
        secrets.update(pattern.findall(keyfile.read_bytes()))
    if (ROOT / 'OpenAI.txt').exists():
        secrets.update(pattern.findall((ROOT / 'OpenAI.txt').read_bytes()))
    for path in paths:
        data = path.read_bytes()
        if any(secret in data for secret in secrets):
            raise RuntimeError('Credential found in release: ' + path.relative_to(ROOT).as_posix())
        if '.runtime' in path.relative_to(ROOT).parts or 'telegram' in path.relative_to(ROOT).parts:
            raise RuntimeError('Private data directory included in release.')


def main():
    paths = release_files()
    verify(paths)
    output = ROOT / '.runtime' / 'release'
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'salavat-editor-2026-11-14.zip'
    manifest = []
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            bundle.writestr(relative, content)
            manifest.append({'path': relative, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        bundle.writestr('BUILD-MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + '.sha256')).write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'files': len(paths), 'bytes': archive.stat().st_size, 'sha256': digest}))


if __name__ == '__main__':
    main()
