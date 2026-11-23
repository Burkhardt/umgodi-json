import json
from pathlib import Path
import re
import subprocess
import sys
import tokenize

import pytest


_vikela_ran = pytest.StashKey[bool]()


def pytest_sessionstart(session):
	_run_vikela(session)


def pytest_collection_modifyitems(session):
	if not session.config.stash.get(_vikela_ran, False):
		_run_vikela(session)


def _run_vikela(session):
	root = Path(__file__).resolve().parent
	result = subprocess.run(
		[sys.executable, "-m", "vikela", ".", "--json"], cwd=root,
		check=False, capture_output=True, text=True,
	)
	if result.stderr:
		sys.stderr.write(result.stderr)
	if result.returncode not in (0, 1):
		pytest.exit(f"Vikela failed with exit code {result.returncode}; pytest was not run.", returncode=1)
	try:
		findings = json.loads(result.stdout)
		if not isinstance(findings, list):
			raise ValueError("Vikela output is not an array")
		expected = set()
		for path in (root / "tests").rglob("test_*.py"):
			with tokenize.open(path) as source:
				for token in tokenize.generate_tokens(source.readline):
					if token.type == tokenize.COMMENT:
						if token.string.startswith("# vikela-expected:"):
							if not re.fullmatch(r"# vikela-expected: RAI012", token.string):
								raise ValueError(f"{path}:{token.start[0]}: unsupported expected finding")
							expected.add((str(path.resolve()), token.start[0], "RAI012"))
		observed = set()
		unexpected = []
		for finding in findings:
			if not isinstance(finding, dict) or not all(key in finding for key in ("path", "line", "rule", "reason")):
				raise ValueError("Malformed Vikela finding")
			key = (finding["path"], finding["line"], finding["rule"])
			if key in expected and key not in observed:
				observed.add(key)
				print(f"Expected Vikela finding: {key[0]}:{key[1]} {key[2]}")
			else:
				unexpected.append(finding)
		missing = expected - observed
	except (ValueError, KeyError, TypeError, OSError, tokenize.TokenError, SyntaxError) as exc:
		pytest.exit(f"Vikela gate could not verify findings: {exc}; pytest was not run.", returncode=1)
	for finding in unexpected:
		print(f"{finding['path']}:{finding['line']} {finding['rule']}: {finding['reason']}", file=sys.stderr)
	for path, line, rule in sorted(missing):
		print(f"Expected Vikela finding missing: {path}:{line} {rule}", file=sys.stderr)
	if unexpected or missing or result.returncode != bool(findings):
		pytest.exit(f"Vikela failed with exit code {result.returncode}; pytest was not run.", returncode=1)
	session.config.stash[_vikela_ran] = True
