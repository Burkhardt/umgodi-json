from pathlib import Path
import pytest
from inqaba import InqConfig
from tests.sequence_runner import allocate_suite_root, suite_variables
from umgodi.config import loads_json5


def test_runs_allocate_distinct_dropbox_roots(monkeypatch, tmp_path):
	config = InqConfig({"TestCloud": "Dropbox", "Cloud": {"Dropbox": str(tmp_path)}})
	monkeypatch.setattr(InqConfig, "load", lambda: config)
	with allocate_suite_root({}) as first, allocate_suite_root({}) as second:
		assert first != second
		assert Path(first).parent == tmp_path
		assert Path(first).name.startswith("RAIkeep.Test.")
	assert not Path(first).exists()
	assert tmp_path.exists()


def test_no_onedrive_fallback(monkeypatch, tmp_path):
	monkeypatch.setattr(InqConfig, "load", lambda: InqConfig({"Cloud": {"OneDrive": str(tmp_path)}}))
	with pytest.raises(RuntimeError, match="TestCloud"):
		allocate_suite_root({})


@pytest.mark.parametrize("data", [{"Cloud": "OneDrive"}, {"Root": "/live/AfricaStage"}])
def test_unsafe_suite_settings_rejected(data):
	with pytest.raises(ValueError): allocate_suite_root(data)


@pytest.mark.parametrize("variables", [{"PitRoot": "/live/AIA"}, {"RootName": "AfricaStage"}, {"cloud": "OneDrive"}, {"root": "/tmp"}])
def test_variables_cannot_escape_owned_root(tmp_path, variables):
	with pytest.raises(ValueError): suite_variables({"Variables": variables}, str(tmp_path))


def test_all_shipped_suites_resolve_inside_owned_root(tmp_path, monkeypatch):
	config = InqConfig({"TestCloud": "Dropbox", "Cloud": {"Dropbox": str(tmp_path)}})
	monkeypatch.setattr(InqConfig, "load", lambda: config)
	for source in (Path(__file__).parent / "suites").glob("*.json5"):
		data = loads_json5(source.read_text())
		variables = suite_variables(data, str(tmp_path))
		assert variables["cloud"] == "Dropbox"
		assert variables["RootName"] == variables["PitRoot"]
		assert Path(variables["PitRoot"]).is_relative_to(tmp_path)
		assert "OneDriveData" not in source.read_text()
