# Local and Engenho verification

Use a dedicated development environment with both editable packages and the
`pytest`, `vikela`, `build`, and `twine` tools. Runtime installations need only Inqaba.
Every pytest run first checks the entire umgodi repository with vikela. It
stops on unexpected findings, missing explicitly annotated RAI012 negative-test
findings, or a failed check. Install the project with its `dev` extras to provide
vikela in the same Python environment as pytest.

From the umbrella checkout:

```sh
.venv-inqaba/bin/python umgodi-json/scripts/preflight.py
```

The script runs both complete test suites, builds wheels and source archives,
validates metadata with Twine, checks source archive documentation/artwork/tests,
and installs the local wheels into a clean environment with no package index.
It checks dependency resolution, installed import origins, version consistency,
persistence initialization and both `jpit` and `umgodi` entry points.
The persistence smoke uses a run-owned directory in the configured Dropbox
root; it reports a skip if the real configuration or drive is unavailable.
The optional declarative sequence suites require explicit opt-in.
The GitHub-hosted release runner installs the native Amafu 4.7.0 binary, but
Amafu does not install or synchronize Dropbox. Cloud tests still skip unless
the runner has the real RAIkeep configuration with `TestCloud` set to Dropbox
and an available Dropbox cloud drive. A runner with those prerequisites is
needed for cloud-backed acceptance.

Artifacts, command output, hashes and a JSON evidence report are retained in the
printed temporary directory. Any failure stops the run. No uploads occur.

Cross-engine tests need a built pits candidate and permission for its normal
OsLib initialization. Their Pits must stay in run-owned Dropbox directories;
ordinary exported JSON may use a system temporary directory.
