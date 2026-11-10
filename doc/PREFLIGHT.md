# Local and Engenho verification

Use a dedicated development environment with both editable packages and the
`pytest`, `build`, and `twine` tools. Runtime installations need only Inqaba.

From the umbrella checkout:

```sh
.venv-inqaba/bin/python umgodi-json/scripts/preflight.py
```

The script runs both complete test suites, builds wheels and source archives,
validates metadata with Twine, checks source archive documentation/artwork/tests,
and installs the local wheels into a clean environment with no package index.
It checks dependency resolution, installed import origins, version consistency,
persistence initialization and both `jpit` and `umgodi` entry points.

Artifacts, command output, hashes and a JSON evidence report are retained in the
printed temporary directory. Any failure stops the run. No uploads occur.

Cross-engine tests need a built pits candidate and permission for its normal
OsLib initialization. A restricted sandbox can cause the C# CLI to reject an
otherwise explicit temporary root. Run release acceptance in the normal isolated
Engenho environment with the required filesystem access.
