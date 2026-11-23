from pathlib import Path
import pytest
from tests.sequence_runner import allocate_suite_root, suite_variables, wait_for_suite_ticket
from umgodi.config import loads_json5


def test_runs_allocate_distinct_cloud_roots(configured_test_cloud):
	_, root = configured_test_cloud
	with allocate_suite_root({}) as first, allocate_suite_root({}) as second:
		assert first != second
		assert Path(first).parent.resolve() == root.resolve()
		assert Path(first).name.startswith("RAIkeep.Test.")
	assert not Path(first).exists()
	assert root.exists()


@pytest.mark.parametrize("data", [{"Cloud": "OneDrive"}, {"Root": "/live/AfricaStage"}])
def test_unsafe_suite_settings_rejected(data):
	with pytest.raises(ValueError): allocate_suite_root(data)


@pytest.mark.parametrize("variables", [{"PitRoot": "/live/AIA"}, {"RootName": "AfricaStage"}, {"cloud": "OneDrive"}, {"root": "/tmp"}])
def test_variables_cannot_escape_owned_root(configured_test_cloud, tmp_path, variables):
	with pytest.raises(ValueError): suite_variables({"Variables": variables}, str(tmp_path))


def test_shell_unsafe_paths_are_rejected(configured_test_cloud, tmp_path):
	with pytest.raises(ValueError, match="unsafe"):
		suite_variables({"Variables": {"PitRoot": "{root}/with space"}}, str(tmp_path))


def test_ticket_wait_cannot_escape_owned_root(pit_test_root, tmp_path):
	with pytest.raises(ValueError, match="inside"):
		wait_for_suite_ticket(pit_test_root, tmp_path)


def test_all_shipped_suites_resolve_inside_owned_root(configured_test_cloud, tmp_path):
	for source in (Path(__file__).parent / "suites").glob("*.json5"):
		data = loads_json5(source.read_text())
		variables = suite_variables(data, str(tmp_path))
		assert variables["cloud"] == configured_test_cloud[0]
		assert variables["RootName"] == variables["PitRoot"]
		assert Path(variables["PitRoot"]).is_relative_to(tmp_path)
		assert "OneDriveData" not in source.read_text()
