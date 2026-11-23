import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("vikela_exit", [0, 1, 2])
@pytest.mark.parametrize("late_collection", [False, True])
def test_pytest_runs_vikela_before_tests(tmp_path, vikela_exit, late_collection):
	root = Path(__file__).resolve().parents[1]
	project = tmp_path / "project" if late_collection else tmp_path
	project.mkdir(exist_ok=True)
	shutil.copyfile(root / "conftest.py", project / "conftest.py")
	package = project / "vikela"
	package.mkdir()
	(package / "__init__.py").write_text("", encoding="utf-8")
	report = json.dumps([{"path": "test_sample.py", "line": 1, "rule": "RAI012", "reason": "violation"}]) if vikela_exit == 1 else "[]"
	(package / "__main__.py").write_text(
		"import sys\n"
		"assert sys.argv[1:] == ['.', '--json']\n"
		"print('vikela ran', file=sys.stderr, flush=True)\n"
		f"print({report!r})\n"
		f"raise SystemExit({vikela_exit})\n",
		encoding="utf-8",
	)
	(project / "test_sample.py").write_text(
		"def test_sample():\n\tprint('test body ran')\n", encoding="utf-8"
	)
	env = os.environ.copy()
	env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(project), env.get("PYTHONPATH"))))
	command = [sys.executable, "-m", "pytest", "-q", "-s"]
	if not late_collection:
		command.append("test_sample.py")
	result = subprocess.run(
		command, cwd=tmp_path, env=env, text=True, stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT, check=False,
	)
	output = result.stdout
	assert output.count("vikela ran") == 1
	if vikela_exit:
		assert result.returncode != 0
		assert f"Vikela failed with exit code {vikela_exit}" in output
		assert "test body ran" not in output
	else:
		assert result.returncode == 0, output
		assert output.index("vikela ran") < output.index("test body ran")


@pytest.mark.parametrize(("reported", "expected", "succeeds"), [
	("RAI012", "RAI012", True),
	(None, "RAI012", False),
	("RAI023", "RAI012", False),
	("RAI012", None, False),
	("RAI021", "RAI021", False),
])
def test_vikela_gate_validates_expected_negative_findings(tmp_path, reported, expected, succeeds):
	root = Path(__file__).resolve().parents[1]
	shutil.copyfile(root / "conftest.py", tmp_path / "conftest.py")
	tests = tmp_path / "tests"
	tests.mkdir()
	source = tests / "test_sample.py"
	marker = f"  # vikela-expected: {expected}" if expected else ""
	source.write_text(f"def test_sample():{marker}\n    pass\n", encoding="utf-8")
	package = tmp_path / "vikela"
	package.mkdir()
	(package / "__init__.py").write_text("", encoding="utf-8")
	findings = [{"path": str(source), "line": 1, "rule": reported, "reason": "test finding"}] if reported else []
	(package / "__main__.py").write_text(
		f"print({json.dumps(findings)!r})\nraise SystemExit({1 if findings else 0})\n", encoding="utf-8",
	)
	env = os.environ.copy()
	env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(tmp_path), env.get("PYTHONPATH"))))
	result = subprocess.run(
		[sys.executable, "-m", "pytest", "-q"], cwd=tmp_path, env=env,
		text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
	)
	assert (result.returncode == 0) is succeeds, result.stdout
	if succeeds:
		assert "Expected Vikela finding:" in result.stdout
