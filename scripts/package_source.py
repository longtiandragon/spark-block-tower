"""Create an allowlisted source bundle, excluding runtime data and credentials."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import json

ROOT = Path(__file__).resolve().parents[1]


def package():
    output = ROOT / "output/SPARK蓝色积木搭塔_源码.zip"
    output.parent.mkdir(exist_ok=True)
    files = []
    for directory in ["backend/spark_freeze", "frontend/src", "frontend/public", "frontend/e2e",
                      "ros", "robot_console", "tools", "config", "scripts", "tests", "docs", "assets/speech"]:
        files.extend(p for p in (ROOT/directory).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo','.tmp'})
    for name in ["README.md", "pyproject.toml", "uv.lock", "Setup.ps1", "Start.ps1", "start.cmd",
                 ".gitignore", ".env.example", "frontend/index.html", "frontend/package.json",
                 "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.ts", "frontend/playwright.config.ts"]:
        files.append(ROOT/name)
    # The imported key is checked locally, never printed or copied into the archive.
    private = ROOT/'data/ai-config.json'
    key = json.loads(private.read_text(encoding='utf-8')).get('key','') if private.exists() else ''
    selected = sorted(set(files))
    for p in selected:
        rel = p.relative_to(ROOT)
        assert not any(x in rel.parts for x in ['data','.env','.venv','node_modules','tmp','output']), rel
        if key and key.encode() in p.read_bytes():raise RuntimeError('Private key detected; source archive not written')
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        for p in selected:archive.write(p,Path('spark-freeze')/p.relative_to(ROOT))
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert 'spark-freeze/assets/speech/manifest.json' in archive.namelist()
    print('Source files:',len(selected), '| Archive bytes:',output.stat().st_size,'| Private config excluded')


if __name__ == '__main__':package()
