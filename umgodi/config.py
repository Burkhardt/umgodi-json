"""Compatibility exports; configuration is owned by Inqaba."""
from inqaba.config import InqConfig
from inqaba.config import DEFAULT_CONFIG_FILE_LOCATION as DEFAULT_CONFIG_FILE_LOCATION
from inqaba.config import missing_configuration_diagnostic as missing_configuration_diagnostic
from inqaba.json5 import loads_json5 as loads_json5

OsConfig = InqConfig
