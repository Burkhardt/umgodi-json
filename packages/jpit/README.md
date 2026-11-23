# jpit (v4.7.1)

`jpit` is the actively maintained command-line interface for
[`umgodi-json`](https://pypi.org/project/umgodi-json/), the successor to the
frozen `jsonpit-python` / `jsonpit` Python project. It depends on the matching
Umgodi library version and exposes the continuing `jpit` and `umgodi` commands.
Python 3.12 or newer is required.

## Installation

For a new CLI installation, use one pipx environment:

```bash
pipx install "umgodi-json==4.7.1"
jpit --version
umgodi --version
```

To upgrade, run `pipx upgrade umgodi-json`. Both commands include the `v` prefix in
their version output, for example `jpit v4.7.1`. These installation commands
apply after publication.

If both distributions were installed into separate pipx environments:

```bash
pipx uninstall umgodi-json
pipx install --force "umgodi-json==4.7.1"
```

Use a virtual environment to embed the library in a Python application. Do not
bypass the operating system's externally managed Python protection.

See the [Getting Started guide](https://github.com/Burkhardt/umgodi-json/blob/main/GettingStarted.md)
and [API reference](https://github.com/Burkhardt/umgodi-json/blob/main/API.md).
