"""Explicit path tests using the real configuration."""

from __future__ import annotations

from inqaba import InqPath
import pytest

from umgodi import Pit, PitItem
from umgodi.cli import main as cli_main

def test_explicit_paths_work_with_real_config(cloud_tenant, capsys) -> None:
	_, tenant = cloud_tenant
	pit_dir = tenant / "LocalPit"

	with Pit.open(InqPath.from_directory(pit_dir)) as pit:
		item = PitItem({"Id": "Item1", "Name": "Local Entity"})
		pit.add(item)
		assert len(pit) == 1

	with Pit.open(InqPath.from_directory(pit_dir)) as pit:
		loaded = pit.get("Item1")
		assert loaded is not None
		assert loaded["Name"] == "Local Entity"

	exit_code = cli_main(["export", str(pit_dir), "--json", "-n"])
	assert exit_code == 0
	assert "Item1" in capsys.readouterr().out

	exit_code = cli_main(["grep", "Local Entity", str(pit_dir), "--json", "-n"])
	assert exit_code == 0
	assert "Item1" in capsys.readouterr().out

	exit_code = cli_main(["grep", "Local Entity", str(pit_dir / "LocalPit.pit"), "--json", "-n"])
	assert exit_code == 0
	assert "Item1" in capsys.readouterr().out


def test_pit_rejects_non_inqpath_and_file_arguments(pit_test_root) -> None:
	directory = pit_test_root / "Person"
	for invalid in (directory, str(directory)):
		with pytest.raises(TypeError, match="InqPath"):
			Pit(invalid, "Person")
		with pytest.raises(TypeError, match="InqPath"):
			Pit.open(invalid)
	for invalid in (InqPath.from_directory(directory / "Person.pit"),):
		with pytest.raises(ValueError, match="directory"):
			Pit(invalid, "Person")
		with pytest.raises(ValueError, match="directory"):
			Pit.open(invalid)
