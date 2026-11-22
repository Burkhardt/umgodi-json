"""CR082: tenant resolution and honest missing-snapshot export failures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from umgodi.cli import main
from umgodi.audit import PitAudit
from umgodi.config import OsConfig


def snapshot(root: Path, name: str = "Object", item_id: str = "CloudItem") -> Path:
	path = root / name / f"{name}.pit"
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps([[{"Id": item_id, "Name": item_id}]]), encoding="utf-8")
	return path


@pytest.fixture
def clouds(cloud_tenant, tmp_path, monkeypatch):
	provider, tenant = cloud_tenant
	monkeypatch.chdir(tmp_path)
	snapshot(tenant)
	return provider, tenant


@pytest.mark.parametrize("root_option", ["-r", "--pitroot", "--root"])
def test_export_uses_configured_default_and_explicit_cloud_override(clouds, tmp_path, capsys, root_option):
	provider, tenant = clouds
	for options, expected in [(["-c", provider], "CloudItem")]:
		assert main(["export", "Object", root_option, tenant.name, "--json", *options]) == 0
		result = capsys.readouterr()
		assert json.loads(result.out)[0]["Id"] == expected
		assert result.err == ""
	assert not (tmp_path / tenant.name).exists()


def test_directory_export_and_timestamp_use_default_cloud(clouds, tmp_path, capsys):
	provider, tenant = clouds
	destination = tmp_path / "exports"
	assert main(["export", "Object", "-r", tenant.name, "-c", provider, "--out-dir", str(destination),
		"--at", "2030-01-01T00:00:00Z"]) == 0
	assert json.loads((destination / "Object.json").read_text())["data"][0]["Id"] == "CloudItem"
	assert "Exported Object" in capsys.readouterr().out


@pytest.mark.parametrize("command", [
	["history", "Object", "CloudItem"],
	["grep", "CloudItem", "Object", "--json"], ["grep", "CloudItem", "--json"],
	["list"], ["export", "Object", "--json"],
	["audit", "--wwwa", "--json"], ["maintain", "--wwwa", "--json"],
	["audit", "Object", "--json"], ["maintain", "Object", "--json"],
])
def test_read_commands_do_not_select_same_named_local_directory(clouds, tmp_path, capsys, monkeypatch, command):
	provider, tenant = clouds
	snapshot(tmp_path / tenant.name, item_id="LocalDecoy")
	inspected = []
	original_inspect = PitAudit.inspect
	def inspect(directory, **kwargs):
		inspected.append(directory)
		return original_inspect(directory, **kwargs)
	monkeypatch.setattr(PitAudit, "inspect", inspect)
	assert main([*command, "-r", tenant.name, "-c", provider]) == 0
	result = capsys.readouterr()
	assert "LocalDecoy" not in result.out
	assert result.err == ""
	if command[0] not in ("audit", "maintain"):
		assert "CloudItem" in result.out or str(tenant) in result.out
	else:
		assert str(tmp_path / tenant.name) not in result.out
	if command[0] == "audit":
		names = ["Person", "Object", "Place", "Activity"] if "--wwwa" in command else ["Object"]
		assert [Path(directory) for directory in inspected] == [tenant / name for name in names]
	assert not list(tenant.rglob("*.flag"))


def test_export_uses_default_when_it_selects_test_cloud(clouds, capsys):
	_, tenant = clouds
	if Path(str(OsConfig.load().get_cloud_root())) != tenant.parent:
		pytest.skip("The default provider differs from TestCloud")
	assert main(["export", "Object", "-r", tenant.name, "--json"]) == 0
	assert json.loads(capsys.readouterr().out)[0]["Id"] == "CloudItem"


@pytest.mark.parametrize("root_form", ["absolute", "relative", "home"])
def test_explicit_local_roots_work_without_config(tmp_path, monkeypatch, capsys, root_form):

	monkeypatch.chdir(tmp_path)

	snapshot(tmp_path / "LocalTenant", item_id="LocalItem")
	root = {"absolute": str(tmp_path / "LocalTenant"), "relative": "./LocalTenant", "home": "~/" + str((tmp_path / "LocalTenant").relative_to(Path.home())) if (tmp_path / "LocalTenant").is_relative_to(Path.home()) else None}[root_form]
	if root is None:
		pytest.skip("Temporary local root is outside the real home directory")
	assert main(["export", "Object", "-r", root, "--json"]) == 0
	assert json.loads(capsys.readouterr().out)[0]["Id"] == "LocalItem"


@pytest.mark.parametrize("output_mode", ["json", "directory"])
@pytest.mark.parametrize("source_form", ["tenant", "directory", "file"])
def test_missing_snapshot_fails_without_output_or_destination_changes(clouds, tmp_path, capsys, output_mode, source_form):
	provider, tenant = clouds
	missing = tenant / "Missing" / "Missing.pit"
	missing.parent.mkdir()
	source = {"tenant": ["Missing", "-r", tenant.name, "-c", provider], "directory": [str(missing.parent)],
		"file": [str(missing)]}[source_form]
	destination = tmp_path / "exports"
	destination.mkdir()
	existing = destination / "Missing.json"
	existing.write_text("preserve existing export", encoding="utf-8")
	mode = {"json": ["--json"], "directory": ["--out-dir", str(destination)],
		"file": ["--out", str(existing)], "jq": ["--jq", "."]}[output_mode]
	assert main(["export", *source, *mode]) == 1
	result = capsys.readouterr()
	assert result.out == ""
	assert "does not exist" in result.err and str(missing) in result.err
	assert existing.read_text() == "preserve existing export"
	assert not list(tenant.rglob("*.flag"))


def test_missing_wwwa_member_fails_without_creating_output_directory(clouds, tmp_path, capsys):
	provider, tenant = clouds
	snapshot(tenant, "Person")
	destination = tmp_path / "not-created"
	assert main(["export", "--wwwa", "-r", tenant.name, "-c", provider, "--out-dir", str(destination)]) == 1
	result = capsys.readouterr()
	assert result.out == "" and "Place.pit" in result.err
	assert not destination.exists()


def test_existing_empty_snapshot_is_a_successful_empty_export(clouds, capsys):
	provider, tenant = clouds
	(tenant / "Object" / "Object.pit").write_text("[]", encoding="utf-8")
	assert main(["export", "Object", "-r", tenant.name, "-c", provider, "--json"]) == 0
	result = capsys.readouterr()
	assert json.loads(result.out) == [] and result.err == ""


def test_complete_wwwa_export_uses_default_cloud(clouds, capsys):
	provider, tenant = clouds
	for name in ("Person", "Place", "Activity"):
		snapshot(tenant, name, item_id=name)
	assert main(["export", "--wwwa", "-r", tenant.name, "-c", provider, "--json"]) == 0
	result = capsys.readouterr()
	assert set(json.loads(result.out)) == {"Person", "Object", "Place", "Activity"}
	assert result.err == ""


def test_explicit_missing_cloud_does_not_fall_back_to_default(clouds, capsys):
	_, tenant = clouds
	assert main(["export", "Object", "-r", tenant.name, "-c", "UnknownDrive", "--json"]) == 1
	result = capsys.readouterr()
	assert result.out == "" and "UnknownDrive" in result.err


@pytest.mark.parametrize("configuration", ["missing", "unavailable"])
def test_no_cloud_never_falls_back_to_local_tenant(tmp_path, monkeypatch, capsys, configuration):
	config = OsConfig.load()
	if config.get_cloud_root() is not None or config.is_config_loaded != (configuration == "unavailable"):
		pytest.skip(f"Requires a host with {configuration} cloud configuration")
	monkeypatch.chdir(tmp_path)
	snapshot(tmp_path / "LocalDecoyTenant", item_id="LocalDecoy")
	assert main(["export", "Object", "-r", "LocalDecoyTenant", "--json"]) == 1
	result = capsys.readouterr()
	assert result.out == ""
	assert "amafu init" in result.err if configuration == "missing" else "default" in result.err


@pytest.mark.parametrize("command", [
	["seed", "Object", "--source", "-"], ["add", "Object", "--source", "-"],
	["delete-item", "Object", "CloudItem"], ["delete-property", "Object", "CloudItem", "Name"],

	["maintain", "Object", "--apply"], ["maintain", "--wwwa", "--apply"],
])
def test_cr069_explicit_write_destination_is_still_required(clouds, tmp_path, capsys, command):
	provider, tenant = clouds
	cloud_file = tenant / "Object" / "Object.pit"
	local_file = snapshot(tmp_path / tenant.name, item_id="LocalDecoy")
	before = cloud_file.read_bytes(), local_file.read_bytes()
	assert main([*command, "-r", tenant.name]) == 1
	result = capsys.readouterr()
	assert "Supply -c <provider> or an explicit local directory path" in result.err
	assert result.out == ""
	assert (cloud_file.read_bytes(), local_file.read_bytes()) == before
	assert not list(tenant.rglob("*.flag"))


def test_missing_first_default_provider_uses_next_available_provider(clouds, capsys):
	_, tenant = clouds
	config = OsConfig.load()
	first = config.get_cloud_root(config.default_cloud_order[0]) if config.default_cloud_order else None
	default = config.get_cloud_root()
	if first is None or Path(str(first)).exists() or default is None or Path(str(default)) != tenant.parent:
		pytest.skip("Requires an unavailable first provider followed by TestCloud")
	assert main(["export", "Object", "-r", tenant.name, "--json"]) == 0
	assert json.loads(capsys.readouterr().out)[0]["Id"] == "CloudItem"
