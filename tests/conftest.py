from pathlib import Path
import os
import shutil
import subprocess
from tempfile import TemporaryDirectory
import warnings

import pytest
from umgodi.config import OsConfig, loads_json5


def _amafu_cloud_root(provider):
	try:
		result = subprocess.run(
			["amafu", "list", "cloud", provider], capture_output=True, text=True, check=False,
		)
	except FileNotFoundError:
		pytest.skip("Cloud tests require Amafu on PATH to locate the configured cloud drive")
	if result.returncode:
		pytest.skip(f"Cannot resolve {provider} through amafu: {result.stderr.strip() or result.stdout.strip()}")
	lines = result.stdout.splitlines()
	if len(lines) != 1 or not Path(lines[0]).is_absolute():
		pytest.fail(f"amafu did not return one absolute {provider} root")
	root = Path(lines[0]).resolve()
	configured = OsConfig.load().get_cloud_root(provider)
	if configured is None:
		pytest.skip(f"Cloud.{provider} is not configured")
	if Path(str(configured)).resolve() != root:
		pytest.fail(f"amafu {provider} root differs from the real configuration")
	if not root.is_dir():
		pytest.skip(f"Cloud tests require the configured {provider} cloud drive to be installed and available")
	return root


def _ensure_dropbox_timing(root):
	parent = root / "JsonPit"
	if not parent.resolve().is_relative_to(root):
		pytest.fail("Dropbox timing directory resolves outside Dropbox")
	destination = parent / "timing.json5"
	if destination.is_file():
		if not destination.resolve().is_relative_to(root):
			pytest.fail("Dropbox timing file resolves outside Dropbox")
		return
	if destination.exists() or destination.is_symlink():
		pytest.fail(f"Dropbox timing path is not a regular file: {destination}")
	onedrive = _amafu_cloud_root("OneDrive")
	source = onedrive / "JsonPit" / "timing.json5"
	if not source.is_file() or not source.resolve().is_relative_to(onedrive):
		pytest.skip("Dropbox timing.json5 is missing and the original OneDrive timing.json5 is unavailable")
	parent.mkdir(parents=True, exist_ok=True)
	with TemporaryDirectory(dir=parent, prefix=".timing-") as directory:
		staged = Path(directory) / "timing.json5"
		shutil.copyfile(source, staged)
		try:
			os.link(staged, destination)
		except FileExistsError:
			if not destination.is_file() or not destination.resolve().is_relative_to(root):
				pytest.fail(f"Dropbox timing path is not a regular file: {destination}")
			return
	warnings.warn(
		"Dropbox has no timing.json5; copied the unchanged OneDrive file. "
		"Calibrate the Dropbox values and rerun the tests for precise timing.",
		pytest.PytestWarning, stacklevel=2,
	)


@pytest.fixture
def configured_test_cloud():
	path = Path.home() / ".config" / "RAIkeep.json5"
	if not path.is_file():
		pytest.skip("Cloud tests require ~/.config/RAIkeep.json5 with TestCloud set to Dropbox and an available Dropbox cloud drive")
	active = OsConfig.find_config_file()
	if active is None or Path(active).resolve() != path.resolve():
		pytest.skip("Cloud tests require the user's active ~/.config/RAIkeep.json5 and an available Dropbox cloud drive")
	provider = loads_json5(path.read_text(encoding="utf-8")).get("TestCloud")
	if provider != "Dropbox":
		pytest.skip("Umgodi Pit tests require Dropbox as the configured TestCloud")
	root = _amafu_cloud_root("Dropbox")
	_ensure_dropbox_timing(root)
	return provider, root


@pytest.fixture
def cloud_tenant(configured_test_cloud):
	provider, root = configured_test_cloud
	with TemporaryDirectory(dir=root, prefix="RAIkeepTest-") as directory:
		yield provider, Path(directory)


@pytest.fixture
def pit_test_root(configured_test_cloud):
	_, root = configured_test_cloud
	root = root.resolve()
	parent = root / "RAIkeepTests" / "umgodi-json"
	if not parent.resolve().is_relative_to(root):
		pytest.fail("Umgodi test directory resolves outside Dropbox")
	parent.mkdir(parents=True, exist_ok=True)
	with TemporaryDirectory(dir=parent, prefix="run-") as directory:
		yield Path(directory)


@pytest.fixture
def csharp_person_pit(pit_test_root):
	source = Path(__file__).parent / "fixtures" / "Person.pit"
	destination = pit_test_root / "Person.pit"
	shutil.copyfile(source, destination)
	return destination
