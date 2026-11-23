"""
Unit and integration tests for the jpit developer CLI and Pit-Grep engine.
"""

from __future__ import annotations

import contextlib
import io
import json
import pytest
import os
import tempfile
from pathlib import Path
from typing import Any

from umgodi.cli import _render_discovery, main

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "Person.pit"


class _CaptureResult:
	def __init__(self, out: str, err: str) -> None:
		self.out = out
		self.err = err


@contextlib.contextmanager
def _capture_output(capsys: Any = None) -> Any:
	if capsys is not None:
		yield capsys
	else:
		f_out = io.StringIO()
		f_err = io.StringIO()
		with contextlib.redirect_stdout(f_out), contextlib.redirect_stderr(f_err):
			yield _CaptureResult(f_out.getvalue(), f_err.getvalue())


@pytest.mark.parametrize("verb", ["put", "set", "get", "rename", "status", "delete"])
def test_removed_verbs_are_rejected(verb):
	with pytest.raises(SystemExit) as error:
		main([verb, "Person"])
	assert error.value.code == 2


def test_cli_grep_living_state(csharp_person_pit: Path, capsys: Any = None) -> None:
	f_out = io.StringIO()
	f_err = io.StringIO()
	with contextlib.redirect_stdout(f_out), contextlib.redirect_stderr(f_err):
		code = main(["grep", "Smalltalk", str(csharp_person_pit)])
	assert code == 0
	out = f_out.getvalue()
	assert "Adele" in out
	assert "Alan" in out


def test_cli_grep_json_output(csharp_person_pit: Path, capsys: Any = None) -> None:
	f_out = io.StringIO()
	f_err = io.StringIO()
	with contextlib.redirect_stdout(f_out), contextlib.redirect_stderr(f_err):
		code = main(["grep", "Goldberg", str(csharp_person_pit), "--json"])
	assert code == 0
	results = json.loads(f_out.getvalue())
	assert len(results) >= 1
	assert any(r["Id"] == "7010" and r["Name"] == "Adele" for r in results)


def test_seed_add_share_sparse_history_and_delete(pit_test_root, tmp_path, capsys):
	pit = str(pit_test_root / "TestPit.pit")
	source = tmp_path / "source.json"
	for verb, payload in [("seed", {"Name": "Alice", "Role": "Architect"}), ("add", {"Role": "Editor"})]:
		source.write_text(json.dumps({"Id": "User1", **payload}))
		assert main([verb, pit, "--source", str(source)]) == 0
	capsys.readouterr()
	assert main(["history", pit, "User1"]) == 0
	history = json.loads(capsys.readouterr().out)
	assert [entry["Role"] for entry in history] == ["Editor", "Architect"]
	assert "Name" not in history[0]
	assert main(["del-prop", pit, "User1", "Name"]) == 0
	assert main(["del", pit, "User1"]) == 0


def test_cli_version_flag(capsys: Any = None) -> None:
	f_out = io.StringIO()
	try:
		with contextlib.redirect_stdout(f_out):
			main(["-v"])
	except SystemExit as ex:
		assert ex.code == 0
	from umgodi import __version__
	out = f_out.getvalue().strip()
	assert out in (f"jpit v{__version__}", f"umgodi v{__version__}")


def test_cli_flexible_option_placement(csharp_person_pit: Path, capsys: Any = None) -> None:
	# Leading option: jpit -r AIA grep Adele <pit>
	f_out1 = io.StringIO()
	with contextlib.redirect_stdout(f_out1):
		code1 = main(["-r", "AIA", "grep", "Adele", str(csharp_person_pit)])
	assert code1 == 0
	assert "Adele" in f_out1.getvalue()

	# Trailing option: jpit grep Adele <pit> --root AIA
	f_out2 = io.StringIO()
	with contextlib.redirect_stdout(f_out2):
		code2 = main(["grep", "Adele", str(csharp_person_pit), "--root", "AIA"])
	assert code2 == 0
	assert "Adele" in f_out2.getvalue()


def test_read_operation_never_creates_directories(pit_test_root, capsys: Any = None) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp_dir:
		non_existent_pit = Path(tmp_dir) / "GhostPit"
		assert not non_existent_pit.exists()

		f_err = io.StringIO()
		with contextlib.redirect_stderr(f_err):
			code = main(["history", str(non_existent_pit), "GhostEntity"])
		assert code == 1
		assert not non_existent_pit.exists(), "Read operation must never create ghost directory!"

		with contextlib.redirect_stderr(f_err):
			code = main(["grep", "Ghost", str(non_existent_pit)])
		assert code == 1
		assert not non_existent_pit.exists(), "Grep must never create ghost directory!"


def test_disallow_manual_modified_or_deleted(pit_test_root, capsys: Any = None) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp_dir:
		pit_dir = Path(tmp_dir) / "TestPit"

		for attribute, value in [("Deleted", True), ("Modified", "2026-01-01T00:00:00Z")]:
			source = Path(tmp_dir) / "invalid.json"
			source.write_text(json.dumps({"Id": "Item1", attribute: value}))
			assert main(["seed", str(pit_dir), "--source", str(source)]) == 1

		# del-prop on protected attribute must fail
		f_err3 = io.StringIO()
		with contextlib.redirect_stderr(f_err3):
			code3 = main(["delete-property", str(pit_dir), "Item1", "Deleted"])
		assert code3 == 1
		assert "Cannot tombstone protected attribute 'Deleted'" in f_err3.getvalue()


def test_cli_list_discovery_and_ls_alias(pit_test_root) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp_dir:
		root = Path(tmp_dir)
		# Create four mock pits to test positional color sequence
		(root / "PitAlpha").mkdir()
		(root / "PitAlpha" / "PitAlpha.pit").write_text("{}", encoding="utf-8")
		(root / "PitBeta").mkdir()
		(root / "PitBeta" / "PitBeta.pit").write_text("{}", encoding="utf-8")
		(root / "PitGamma").mkdir()
		(root / "PitGamma" / "PitGamma.pit").write_text("{}", encoding="utf-8")
		(root / "PitDelta").mkdir()
		(root / "PitDelta" / "PitDelta.pit").write_text("{}", encoding="utf-8")

		# 1. Test jpit list -r <root>
		f_out = io.StringIO()
		with contextlib.redirect_stdout(f_out):
			code = main(["list", "-r", str(root)])
		assert code == 0
		out = f_out.getvalue()
		assert "PitAlpha" in out
		assert "PitBeta" in out
		assert "PitGamma" in out
		assert "PitDelta" in out

		alias = io.StringIO()
		with contextlib.redirect_stdout(alias):
			assert main(["ls", "-r", str(root)]) == 0
		assert alias.getvalue() == out

		# 3. Test jpit list -r <root> delegation
		f_out_list = io.StringIO()
		with contextlib.redirect_stdout(f_out_list):
			code = main(["list", "-r", str(root)])
		assert code == 0
		assert "PitAlpha" in f_out_list.getvalue()

		# 4. Test jpit -r <root> -h dynamic help line with positional colors
		f_out_help = io.StringIO()
		f_out_help.isatty = lambda: True
		old_term = os.environ.get("TERM")
		old_no_color = os.environ.pop("NO_COLOR", None)
		try:
			os.environ["TERM"] = "xterm-256color"
			with contextlib.redirect_stdout(f_out_help):
				code = main(["-r", str(root), "-h"])
		finally:
			if old_term is None:
				os.environ.pop("TERM", None)
			else:
				os.environ["TERM"] = old_term
			if old_no_color is not None:
				os.environ["NO_COLOR"] = old_no_color

		assert code == 0
		help_out = f_out_help.getvalue()
		assert "PitNames" in help_out
		assert str(root) in help_out
		assert "PitAlpha" in help_out
		assert "PitBeta" in help_out
		# 3rd option (idx 2) is blue (\033[94m), 4th option (idx 3) is red (\033[91m)
		from umgodi.cli import C_BRIGHT_BLUE, C_BRIGHT_RED
		assert C_BRIGHT_BLUE in help_out
		assert C_BRIGHT_RED in help_out

		# 5. Verify main help does NOT display (CR024)
		assert "(CR024)" not in help_out

		# 6. Verify jpit --retain-window -h detailed help and hyperlinks
		f_out_rw = io.StringIO()
		with contextlib.redirect_stdout(f_out_rw):
			code_rw = main(["--retain-window", "-h"])
		assert code_rw == 0
		rw_out = f_out_rw.getvalue()
		assert "Option:" in rw_out
		assert "--retain-window" in rw_out
		assert "CR024" in rw_out
		assert "doc/CR/CR024_AIA_to_RAIkeep_Ephemeral_Flag_Self_Cleanup.md:45" in rw_out
		assert "#L45" in rw_out
		assert "#3---retain-window-compatibility-exception" in rw_out


def test_general_help_shows_wwwa_pit_status_only_when_explicit_or_contextual(pit_test_root) -> None:
	from umgodi.icons import Icons

	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp_dir:
		root = Path(tmp_dir)
		weather = root / "Weather"
		weather.mkdir()

		bare = io.StringIO()
		with contextlib.redirect_stdout(bare):
			assert main(["--help", "-n"]) == 0
		assert all(f"{name}.pit" not in bare.getvalue() for name in ("Person", "Object", "Place", "Activity"))

		weather_help = io.StringIO()
		with contextlib.redirect_stdout(weather_help):
			assert main(["--help", "-r", str(weather), "-n"]) == 0
		assert "Person.pit" not in weather_help.getvalue()

		person_dir = root / "Person"
		person_dir.mkdir()
		person_file = person_dir / "Person.pit"
		person_file.write_text("{}", encoding="utf-8")

		contextual = io.StringIO()
		with contextlib.redirect_stdout(contextual):
			assert main(["--help", "-r", str(root), "-n"]) == 0
		assert f"{Icons.INFO} Person".ljust(15) + f"  {Icons.SUCCESS}   {person_file}" in contextual.getvalue()
		assert f"{Icons.INFO} Activity".ljust(15) + f"  {Icons.NOT_AVAILABLE}" in contextual.getvalue()

		explicit = io.StringIO()
		with contextlib.redirect_stdout(explicit):
			assert main(["--help", "--wwwa", "-n"]) == 0
		assert f"{Icons.INFO} Person".ljust(15) + f"  {Icons.NOT_AVAILABLE}   Person.pit" in explicit.getvalue()
		assert f"{Icons.INFO} Object".ljust(15) + f"  {Icons.NOT_AVAILABLE}   Object.pit" in explicit.getvalue()
		assert f"{Icons.INFO} Place".ljust(15) + f"  {Icons.NOT_AVAILABLE}   Place.pit" in explicit.getvalue()
		assert f"{Icons.INFO} Activity".ljust(15) + f"  {Icons.NOT_AVAILABLE}   Activity.pit" in explicit.getvalue()


def test_discovery_output_uses_trailing_directory_separator() -> None:
	directory = Path("/tmp/diagram-fixture")
	output = _render_discovery("ParityCloud", directory, ["Activity"], all_clouds=True)
	assert output == "[ParityCloud] (/tmp/diagram-fixture/): 1 pit(s) found\n  Activity"


def test_pits_and_umgodi_query_the_same_fragments(pit_test_root, csharp_person_pit, capsys):
	import shutil
	import subprocess

	assembly = Path(__file__).resolve().parents[2] / "PitSeeder/pits/bin/Debug/net10.0/pits.dll"
	if not assembly.is_file() or not shutil.which("dotnet"):
		pytest.skip("Requires the local pits candidate and the .NET runtime")
	path = pit_test_root / "Person" / "Person.pit"
	path.parent.mkdir()
	shutil.copyfile(csharp_person_pit, path)
	before = path.read_bytes()
	for args in (["grep", "^adele$", str(path), "-i", "-e", "-p", "Name", "--json"],
			["history", str(path), "7010"]):
		assert main(args) == 0
		python_rows = json.loads(capsys.readouterr().out)
		native = subprocess.run(["dotnet", str(assembly), *args], capture_output=True, text=True)
		assert native.returncode == 0, native.stderr
		native_rows = json.loads(native.stdout)
		assert native_rows and len(native_rows) == len(python_rows)
		for actual, expected in zip(native_rows, python_rows, strict=True):
			# The serializers allow equivalent ISO timestamps with different fractional precision.
			from umgodi.canonical import parse_iso_timestamp
			assert parse_iso_timestamp(actual.pop("Modified")) == parse_iso_timestamp(expected.pop("Modified"))
			assert actual == expected
	assert path.read_bytes() == before
	assert list(path.parent.iterdir()) == [path]
