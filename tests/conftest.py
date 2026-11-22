from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from umgodi.config import OsConfig, loads_json5


@pytest.fixture
def configured_test_cloud():
	path = Path.home() / ".config" / "RAIkeep.json5"
	if not path.is_file():
		pytest.skip("Cloud test requires ~/.config/RAIkeep.json5")
	active = OsConfig.find_config_file()
	if active is None or Path(active).resolve() != path.resolve():
		pytest.skip("Cloud test requires the user's active RAIkeep.json5")
	provider = loads_json5(path.read_text(encoding="utf-8")).get("TestCloud")
	if not provider:
		pytest.skip("TestCloud is unspecified in RAIkeep.json5")
	root = OsConfig.load().get_cloud_root(provider)
	if root is None or not Path(root).is_dir():
		pytest.skip(f"Configured TestCloud {provider} is unavailable")
	return provider, Path(root)


@pytest.fixture
def cloud_tenant(configured_test_cloud):
	provider, root = configured_test_cloud
	with TemporaryDirectory(dir=root, prefix="RAIkeepTest-") as directory:
		yield provider, Path(directory)
