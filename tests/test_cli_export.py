from inqaba import InqPath
import contextlib
import io
import json
from pathlib import Path

import pytest

from umgodi import Pit, PitItem
from umgodi.cli import main


def run(*args: str) -> tuple[int, str, str]:
	output, error = io.StringIO(), io.StringIO()
	with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
		code = main(list(args))
	return code, output.getvalue(), error.getvalue()


def seed(root: Path, name: str, item: dict) -> None:
	with Pit.open(InqPath.from_directory(root / name), read_only=False, unflagged=True) as pit:
		pit.add(PitItem(item))


def test_export_real_pit_in_configured_test_cloud(cloud_tenant) -> None:
	provider, tenant = cloud_tenant
	with Pit.open("Person", cloud=provider, root=tenant.name, unflagged=True) as pit:
		assert Path(str(pit.canonical_file)).resolve() == (tenant / "Person" / "Person.pit").resolve()
		assert pit.add(PitItem({"Id": "CloudCustomer", "Name": "Example"}))
	assert (tenant / "Person" / "Person.pit").is_file()
	code, output, error = run("export", "Person", "-c", provider, "-r", tenant.name, "--json")
	assert code == 0, error
	assert json.loads(output)[0]["Id"] == "CloudCustomer"


def test_directory_export_uses_the_pit_name_and_preserves_source(pit_test_root: Path, tmp_path: Path) -> None:
	seed(pit_test_root, "Person", {"Id": "Customer1", "Name": "Example"})
	source = pit_test_root / "Person" / "Person.pit"
	before = source.read_bytes()
	destination = tmp_path / "exports"
	code, output, error = run("export", "Person", "-r", str(pit_test_root), "--out-dir", str(destination))
	assert code == 0, error
	assert json.loads((destination / "Person.json").read_text())[0]["Id"] == "Customer1"
	assert "Exported Person" in output
	assert source.read_bytes() == before
	assert not list((pit_test_root / "Person").glob("*.flag"))


def test_stdout_json(pit_test_root: Path) -> None:
	seed(pit_test_root, "Person", {"Id": "Customer1"})
	code, output, error = run("export", "Person", "--pitroot", str(pit_test_root), "--json")
	assert code == 0, error
	assert json.loads(output)[0]["Id"] == "Customer1"
@pytest.mark.parametrize("mode", [[], ["--json", "--out-dir"]])
def test_conflicting_or_missing_modes_fail_before_io(tmp_path: Path, mode: list[str]) -> None:
	args = ["export", "Person", "-r", str(tmp_path)]
	for token in mode:
		args.append(token)
		if token != "--json": args.append(str(tmp_path / "exports"))
	code, _, error = run(*args)
	assert code == 1
	assert "exactly one output mode" in error
	assert list(tmp_path.iterdir()) == []


def test_point_in_time_directory_export_uses_the_csharp_envelope(pit_test_root: Path, tmp_path: Path) -> None:
	seed(pit_test_root, "Person", {"Id": "Customer1"})
	destination = tmp_path / "exports"
	code, _, error = run("export", "Person", "-r", str(pit_test_root), "--out-dir", str(destination),
		"--at", "2030-01-01T02:00:00+02:00")
	assert code == 0, error
	document = json.loads((destination / "Person.json").read_text())
	assert document["_export"]["at"] == "2030-01-01T00:00:00.0000000Z"
	assert document["_export"]["exported"].endswith("Z")
	assert document["data"][0]["Id"] == "Customer1"


def test_wwwa_directory_export_resolves_references_like_pits(pit_test_root: Path, tmp_path: Path) -> None:
	seed(pit_test_root, "Person", {"Id": "Customer1"})
	seed(pit_test_root, "Object", {"Id": "Order1"})
	seed(pit_test_root, "Place", {"Id": "Office1"})
	seed(pit_test_root, "Activity", {"Id": "CreateOrder1", "Who": {"Owner": "Customer1"},
		"What": {"Orders": ["Order1"], "Missing": "Unknown"}, "Where": {"Office": "Office1"}})
	destination = tmp_path / "exports"
	code, _, error = run("export", "--wwwa", "-r", str(pit_test_root), "--out-dir", str(destination))
	assert code == 0, error
	document = json.loads((destination / "wwwa.json").read_text())
	assert set(document) == {"Person", "Object", "Place", "Activity"}
	activity = document["Activity"][0]
	assert activity["Owner"]["Id"] == "Customer1"
	assert activity["Orders"][0]["Id"] == "Order1"
	assert activity["Office"]["Id"] == "Office1"
	assert activity["What"] == {"Missing": "Unknown"}
	assert "Who" not in activity and "Where" not in activity
