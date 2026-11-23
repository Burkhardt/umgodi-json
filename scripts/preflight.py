"""Build, inspect, and fresh-install the Inqaba/Umgodi candidate pair.

Run with a development interpreter containing build, twine and pytest.
Artifacts and evidence are retained in the printed temporary directory.
No distribution is uploaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import venv
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--inqaba", type=Path, default=ROOT.parent / "inqaba")
	args = parser.parse_args()
	inqaba = args.inqaba.resolve()
	work = Path(tempfile.mkdtemp(prefix="inqaba-umgodi-preflight-"))
	dist = work / "dist"
	env = os.environ.copy()
	env.pop("PYTHONPATH", None)
	env.pop("PYTHONHOME", None)
	evidence = {"status": "running", "directory": str(work), "commands": []}

	def run(command, cwd=work, command_env=env):
		result = subprocess.run([str(a) for a in command], cwd=cwd, env=command_env, text=True, capture_output=True)
		evidence["commands"].append({"argv": [str(a) for a in command], "cwd": str(cwd), "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
		print(result.stdout, end="")
		if result.returncode:
			print(result.stderr, file=sys.stderr)
			raise RuntimeError(f"Command failed: {command}")
		return result.stdout.strip()

	try:
		test_env = {**env, "PYTHONPATH": os.pathsep.join(map(str, (inqaba, ROOT, ROOT.parent / "vikela")))}
		for package in (inqaba, ROOT):
			run([sys.executable, "-m", "pytest", "tests", "-q", "-rs"], cwd=package, command_env=test_env)
			run([sys.executable, "-m", "build", "--outdir", dist], cwd=package)
		run([sys.executable, "-m", "twine", "check", *sorted(dist.iterdir())])
		for archive in dist.glob("*.tar.gz"):
			with tarfile.open(archive) as tar:
				names = tar.getnames()
				for required in ("README.md", "pyproject.toml"):
					assert any(n.endswith("/" + required) for n in names), (archive, required)
				for suffix in (".puml", ".svg"):
					assert any(n.endswith(suffix) for n in names), (archive, suffix)
				assert any("/tests/" in n for n in names), archive
		wheels = sorted(dist.glob("*.whl"))
		assert len(wheels) == 2
		clean = work / "installed"
		venv.EnvBuilder(with_pip=True).create(clean)
		bin_dir = clean / ("Scripts" if os.name == "nt" else "bin")
		python = bin_dir / ("python.exe" if os.name == "nt" else "python")
		run([python, "-m", "pip", "install", "--no-index", "--find-links", dist, "umgodi-json"])
		run([python, "-m", "pip", "check"])
		run([python, "-I", "-c", """import importlib.metadata as m
import pathlib, subprocess, tempfile, sys
import inqaba, umgodi
from inqaba import InqConfig, InqPath, InqRelPath, InqFile
from umgodi.config import loads_json5
assert pathlib.Path(inqaba.__file__).is_relative_to(sys.prefix)
assert pathlib.Path(umgodi.__file__).is_relative_to(sys.prefix)
assert m.version('inqaba') == inqaba.__version__
assert m.version('umgodi-json') == umgodi.__version__
for name in ('inqaba', 'umgodi-json'):
    for requirement in m.requires(name) or []:
        if 'extra ==' not in requirement:
            assert not any(tool in requirement.lower() for tool in ('pytest', 'twine', 'ruff', 'mypy', 'build'))
config = pathlib.Path.home() / '.config' / 'RAIkeep.json5'
active = InqConfig.find_config_file()
if not config.is_file() or active is None or pathlib.Path(active).resolve() != config.resolve():
    print('SKIPPED persistence smoke: real RAIkeep configuration is unavailable')
elif loads_json5(config.read_text(encoding='utf-8')).get('TestCloud') != 'Dropbox':
    print('SKIPPED persistence smoke: TestCloud is not Dropbox')
else:
    try:
        result = subprocess.run(['amafu', 'list', 'cloud', 'Dropbox'], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f'SKIPPED persistence smoke: Dropbox is unavailable: {error}')
    else:
        lines = result.stdout.splitlines()
        if len(lines) != 1 or not pathlib.Path(lines[0]).is_absolute():
            raise RuntimeError('amafu did not return one absolute Dropbox root')
        root = pathlib.Path(lines[0]).resolve()
        configured = InqConfig.load().get_cloud_root('Dropbox')
        if configured is None or pathlib.Path(str(configured)).resolve() != root:
            raise RuntimeError('amafu Dropbox root differs from the real configuration')
        if not root.is_dir():
            print('SKIPPED persistence smoke: Dropbox is unavailable')
        else:
            parent = root / 'RAIkeepTests' / 'umgodi-json'
            if not parent.resolve().is_relative_to(root):
                raise RuntimeError('Test directory resolves outside Dropbox')
            parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=parent, prefix='preflight-') as tmp:
                with umgodi.Pit(InqPath(tmp), 'Smoke', read_only=False, unflagged=True) as pit:
                    assert isinstance(pit.pit_dir, InqPath)
            print('Installed-wheel persistence smoke passed on Dropbox')
print('Installed-wheel imports and metadata checks passed')
"""])
		with (ROOT / "pyproject.toml").open("rb") as stream:
			version = tomllib.load(stream)["project"]["version"]
		for command in ("jpit", "umgodi"):
			assert run([bin_dir / command, "--version"]) == f"{command} v{version}"
		evidence["artifacts"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dist.iterdir()}
		evidence["status"] = "passed"
	finally:
		(work / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
		print(f"Preflight evidence: {work / 'evidence.json'}")


if __name__ == "__main__":
	main()
