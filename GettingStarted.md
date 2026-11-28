# Getting Started with Python umgodi-json (v4.7.3)

This guide provides a practical walkthrough for using **`umgodi`** from Python—either embedded as a library in your services, AI agent frameworks, and data pipelines, or as the standalone **`jpit`** CLI for command-line workflows.

It is based on `umgodi` v4.7.3, featuring 100% lockstep parity with C# `JsonPit` in [RAIkeep](https://github.com/Burkhardt/RAIkeep).

---

## 1. Purpose and Mental Model

`umgodi` is a cloud-first, file-based, distributed replicated storage engine for `Id`-identified items that need to be shared, synchronized, and reloaded across machines and processes without introducing a database daemon.

### The Core Concepts:
- **`Pit`**: A named container directory stored on disk (e.g. `Person/Person.pit`).
- **`PitItem`**: An `Id`-identified JSON object with lifecycle metadata (`Modified`, `Deleted`, `Note`) and arbitrary properties.
- **`PitItems`**: The immutable version history of sparse change fragments for an item key.

### Mental Model:
- A `Pit` is closer to a synchronized document store than a SQL table.
- Updates are **append-only sparse fragments**, not blind in-memory overwrites.
- Coordination happens through synchronized cloud storage (OneDrive, Dropbox, GoogleDrive, ICloudDrive) using lease flags and change files.
- **[Asynchronous persistence with eventual durability](https://github.com/Burkhardt/RAIkeep/blob/main/MANIFESTO.md#asynchronous-persistence-with-eventual-durability)**: request handling happens in memory; synchronization is explicit via `save()` or context manager exit.

---

## 3. Installing the CLI or Python Library

For the CLI, install one pipx environment that supplies both commands:

```bash
pipx install "umgodi-json==4.7.3"
# For an existing environment:
pipx upgrade umgodi-json
jpit --version
umgodi --version
```

The outputs are `jpit v4.7.3` and `umgodi v4.7.3`. If a separate `jpit` pipx
environment already exists, run `pipx uninstall jpit`, followed by
`pipx install --force "umgodi-json==4.7.3"` to restore both command links.

Ubuntu/Debian and Homebrew may reject system-wide pip installs as externally
managed. Use pipx for the CLI, or an isolated virtual environment for library use:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "umgodi-json==4.7.3"
```

---

## 4. Configuration & Simplified Cloud Shortcuts

`umgodi` automatically reads your cloud provider roots from `~/.config/RAIkeep.json5` (with fallback to `~/.config/jsonpit.json5` or `$JSONPIT_CONFIG`).

### Zero-Friction Setup via [`amafu`](https://www.nuget.org/packages/Amafu)

On macOS and Linux, enable your cloud clients (iCloud, OneDrive, Google Drive, Dropbox), then use [`amafu`](https://www.nuget.org/packages/Amafu) to auto-detect your local sync folders and create clean, unified shortcuts in `~/.CloudStorage/`:

```bash
# 1. Preview detected roots and proposed shortcuts:
amafu detect --create-links --dry-run

# 2. Initialize configuration AND create shortcuts in ~/.CloudStorage/:
amafu init --create-links
```

If you already have a configuration and just want to create the shortcuts:
```bash
amafu detect --create-links
```

This creates convenient, unified symbolic links:
- `~/.CloudStorage/ICloudDrive` $\rightarrow$ `~/Library/Mobile Documents/com~apple~CloudDocs/`
- `~/.CloudStorage/GoogleDrive` $\rightarrow$ `~/Library/CloudStorage/GoogleDrive-.../My Drive/`
- `~/.CloudStorage/OneDrive` $\rightarrow$ `~/Library/CloudStorage/OneDrive-.../`
- `~/.CloudStorage/Dropbox` $\rightarrow$ `~/Library/CloudStorage/Dropbox/`

You never have to search for obscure operating system cloud paths again. Your real files stay safely in their original cloud folders, and your Python code or terminal sessions can easily use `~/.CloudStorage/<provider>` or reference the provider name directly (e.g. `cloud="ICloudDrive"`).

---

## 5. First Steps with the Python Library

### Basic End-to-End Example
Here is the recommended Python idiom using the `Pit.open()` context manager:

```python
from umgodi import Pit, PitItem

# 1. Open the Pit using the context manager
# (Acquires a process activity window, loads state, and auto-saves on exit)
with Pit.open("person", cloud="GoogleDrive") as people:
    
    # 2. Create an entity with Id
    max_person = PitItem("Max")
    max_person["Email"] = "max@example.org"
    max_person["Phone"] = "+27-82-000-0000"
    max_person["ComPref"] = ["WhatsApp", "Email"]
    
    # 3. Add to Pit
    people.add(max_person)
    print(f"Added {max_person.id} to Pit '{people.pit_name}'")

# --- On context manager exit, people.save() is called automatically ---

# 4. Reopen and query
with Pit.open("person", cloud="GoogleDrive", read_only=True) as people:
    loaded_max = people["Max"]
    print(f"Loaded: {loaded_max.id} -> Email: {loaded_max['Email']}, Phone: {loaded_max['Phone']}")
    print(f"Channels: {loaded_max['ComPref']}")
```

### Updating an Existing Item
Looking up an entity returns a **live reference**. Mutating that reference records sparse modifications directly:

```python
with Pit.open("person", cloud="GoogleDrive") as people:
    max_person = people["Max"]
    
    # Direct indexer updates append sparse fragments
    max_person["Phone"] = "+27-82-111-2222"
    max_person["Instagram"] = "max.africastage"
    
    # Nested dictionaries are deep-merged
    max_person["Address"] = {
        "Street": "42 Long Street",
        "City": "Cape Town",
        "Country": "South Africa"
    }
```

### Removing Attributes (Tombstones)
`umgodi` never mutates history destructively. Deleting a property appends a **null tombstone** that masks previous values in the projection while preserving time travel:

```python
with Pit.open("person", cloud="GoogleDrive") as people:
    max_person = people["Max"]
    
    # Remove a top-level property
    max_person.delete_property("Instagram")
    
    # Remove a nested path
    max_person.delete_property_path("Address.Street")
```

### Deleting an Entity
```python
with Pit.open("person", cloud="GoogleDrive") as people:
    # Tombstones the entire entity
    del people["Max"]
    # or: people.delete_item("Max", by="Operator")
```

### Time Travel and Historical Auditing
Because every modification is an immutable timestamped fragment, you can inspect historical states:

```python
with Pit.open("person", cloud="GoogleDrive", read_only=True) as people:
    # Inspect raw version history
    history = people.history["Max"]
    for frag in history.history:
        print(f"[{frag.timestamp}] {frag.data}")
    
    # Reconstruct the entity as it existed at a past point in time
    past_state = people.get_at("Max", at="2026-10-01T12:00:00Z")
    if past_state:
        print("Past Phone:", past_state.get("Phone"))
```

---

## 6. Alternative: First Steps with the `jpit` CLI

If you prefer to operate directly from the command line without writing Python code, the `jpit` CLI provides high-performance data seeding, mutation, and inspection.

### Step 1: Seed Initial Data
Create a JSON or JSON5 file (`people.json5`):

```json5
[
  {
    Id: "Max",
    Email: "max@example.org",
    Phone: "+27-82-000-0000",
    ComPref: ["WhatsApp", "Email"]
  },
  {
    Id: "Adele",
    Email: "adele@example.org",
    Role: "Software Architect"
  }
]
```

Seed the entities into a Pit named `person`:
```bash
jpit seed person --source people.json5 -r ./tenant
```

### Step 2: Read Entities
Read the fragment history for one entity:
```bash
jpit history person Max -r ./tenant
```

Get only a specific property:
```bash
jpit export person --json -r ./tenant | jq '.[] | select(.Id == "Max") | .Email'
```

List all active entities in the pit:
```bash
jpit list -r ./tenant
```

### Step 3: Sparse Mutations (Anti-Read-Modify-Write)
Update attributes without rewriting the entire entity:

```bash
echo '{"Id":"Max","Phone":"+27-82-111-2222"}' | jpit add person --source - --patch -r ./tenant
```

Add a nested object:
```bash
echo '{"Id":"Max","Address":{"City": "Cape Town", "Country": "South Africa"}}' | jpit add person --source - --patch -r ./tenant
```

Tombstone an attribute:
```bash
jpit delete-property person Max Email -r ./tenant
```

### Step 4: Semantic Search ("Pit-Grep")
Search across living entity states using ripgrep-style speed:

```bash
jpit grep "Cape Town" person
```

Filter by property and output structured JSON:
```bash
jpit grep --property Role "Architect" person --json
```

### Step 5: Exporting & Piping with `jq`
Pipe living state directly into `jq`:
```bash
jpit export person --jq '.[].Id'
```

---

## 7. 100% C# / `pits` Cross-Language Interoperability

Every Pit created or updated by Python `umgodi` is 100% binary- and schema-compatible with C# `JsonPit` and the `pits` CLI.

Write an entity attribute with Python `jpit`:
```bash
echo '{"Id":"Max","Status":"Online"}' | jpit add person --source - --patch -r ./tenant
```

Read it immediately with C# `pits`:
```bash
pits history person Max -r ./tenant
```

Both engines enforce identical timestamp parsing, canonical key ordering, lease flag acquisition, receipt grace periods, and sparse delta projection.

---

## 8. Alternative Installation & Troubleshooting

### Shell PATH troubleshooting
If your shell reports `command not found: jpit`, ensure that `~/.local/bin` is in your shell `PATH`:
```bash
echo 'export PATH="$HOME/.local/bin:$HOME/.dotnet/tools:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Running the tests from the RAIkeep checkout

Use the release virtual environment for both dependency installation and pytest.
From the umbrella directory, run:

```zsh
.release-venv/bin/python -m pip install -e './umgodi-json[dev]'
.release-venv/bin/python -m pytest umgodi-json/tests -n 4 --durations=20 -rs
```

Four workers are the default for Umgodi, including during release validation. They run independent tests concurrently. Dropbox-backed tests use the
configured test drive and its calibrated ticket windows. Compare the final
pass and skip counts as well as the elapsed time. For a sequential comparison,
replace `-n 4` with `-n 0`.
