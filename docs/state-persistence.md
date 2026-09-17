<!-- Nav: ← [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md) | → [state.py](../custom_components/climate_advisor/state.py) -->

# State Persistence — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What file does operational state persist to, and where is the path defined? | `climate_advisor_state.json` in the HA config root; path assembled from `STATE_FILE` constant in `const.py`. | [JSON Serialization](#json-serialization) |
| How does state survive HA restarts without corruption from a mid-write crash? | Atomic write: serialize to a uniquely-named `.tmp` file in the same directory, then `os.replace()` to rename it over the target. The original is never modified until the new file is complete. | [Atomic Write Pattern](#atomic-write-pattern) |
| What happens when the state file has a mismatched version or is malformed? | `load()` logs a WARNING and returns an empty dict — the coordinator starts fresh rather than attempting partial migration. | [Version Guard](#version-guard) |
| Where does LearningState (thermal model, records, suggestions) persist, and is that this module's responsibility? | `LearningState` persists to `climate_advisor_learning.json` and is owned entirely by `LearningEngine` in `learning.py`. `state.py` does not touch that file. | [Scope](#scope) |
| What is STATE_VERSION and when does it change? | Currently `1`. It is compared on load; any mismatch discards the file and starts fresh. It must be incremented whenever the state dict schema changes in a breaking way. | [Version Guard](#version-guard) |

## Scope

**Owns:**
- `StatePersistence` class: atomic load and save of the coordinator's operational state dict to `climate_advisor_state.json`
- Temp-file lifecycle: creation, rename, and cleanup of `climate_advisor_state_*.tmp` files
- Version guard on load: reject files whose `version` field does not match `STATE_VERSION`
- `delete()`: removes the state file and any leftover temp files (used on integration unload/reset)

**Explicitly does NOT own:**
- `LearningState` — lives in `learning.py`; persists to `climate_advisor_learning.json` via `LearningEngine`
- The schema of the state dict — the coordinator (`coordinator.py`) is responsible for what keys are written and read
- Nothing else — file permissions (`0o600` via `os.chmod` after `os.replace()`, guarded by `hasattr(os, "chmod")` for cross-platform safety) ARE set by this module; see Invariants
- Migration logic — version mismatches are handled by discarding and starting fresh; no field-level upgrade path exists in `state.py`

## Responsibilities

- Provide `load() -> dict`: read `climate_advisor_state.json`, validate it is a JSON object, enforce `STATE_VERSION`, return `{}` on any failure
- Provide `save(state: dict)`: inject `version`, serialize to JSON, write atomically via `.tmp` + `os.replace()`
- Provide `delete()`: remove the state file and any orphaned `.tmp` files from the same directory
- Log `WARNING` for non-fatal failures (corrupt file, wrong version) and `ERROR` for write failures

## Interfaces

```python
class StatePersistence:
    def __init__(self, config_dir: Path) -> None: ...

    def load(self) -> dict[str, Any]:
        """Load state from disk. Returns empty dict on missing or corrupt file."""

    def save(self, state: dict[str, Any]) -> None:
        """Write state to disk atomically. Mutates state in place to add 'version'."""

    def delete(self) -> None:
        """Remove state file and any leftover .tmp files."""
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `StatePersistence.__init__` | `ClimateAdvisorCoordinator.__init__` | Constructed once with the HA config directory path |
| `StatePersistence.load` | `_async_save_state` (coordinator, on startup) | Restore coordinator state after HA restart |
| `StatePersistence.save` | `_async_save_state` (coordinator, periodic + on change) | Persist operational state to survive restarts |
| `StatePersistence.delete` | `async_unload_entry` (\_\_init\_\_.py) | Clean up on integration removal |

## Data Structures

`StatePersistence` itself holds no domain data — it is a pure I/O adapter. The state dict it serializes is assembled by the coordinator. Its on-disk format is:

```json
{
  "version": 1,
  "<coordinator-key>": "<value>",
  "..."
}
```

**File:** `{hass.config.config_dir}/climate_advisor_state.json`
**Constant:** `STATE_FILE = "climate_advisor_state.json"` (defined in `const.py`)
**Format:** UTF-8 JSON, 2-space indent
**Serialization fallback:** `default=str` — non-serializable values (e.g., `datetime`) are coerced to strings rather than raising

### Version Guard

`STATE_VERSION = 1` (module-level constant in `state.py`). On `load()`:
- If the file does not exist: return `{}`
- If the JSON is not a dict: log WARNING, return `{}`
- If `data["version"] != STATE_VERSION`: log WARNING with both versions, return `{}`

There is no migration path in `state.py`. Any breaking schema change requires bumping `STATE_VERSION` and accepting that existing state is discarded on first load.

### Atomic Write Pattern

`save()` sequence:
1. Inject `state["version"] = STATE_VERSION`
2. Serialize to JSON string (fail fast: log ERROR and return if serialization fails)
3. `tempfile.mkstemp(dir=same_dir, prefix="climate_advisor_state_", suffix=".tmp")` — unique temp file in the same directory ensures `os.replace()` is atomic on POSIX (same filesystem)
4. Write serialized JSON to the temp fd via `os.fdopen`
5. `os.replace(tmp_path, state_path)` — atomic rename; original file untouched until this line succeeds
6. On `OSError` at step 4 or 5: log ERROR, attempt `os.unlink(tmp_path)` (suppressed if that also fails)

## Invariants

1. **The original state file is never partially overwritten.** All writes go to a `.tmp` file; only `os.replace()` makes the new content visible.
2. **`load()` never raises.** All `json.JSONDecodeError` and `OSError` exceptions are caught; the method always returns a `dict`.
3. **`save()` never raises.** All exceptions are caught and logged; callers do not need try/except around `save()`.
4. **Version is always written by `save()`, never by the caller.** The coordinator passes a state dict without a `version` key; `save()` injects it.
5. **No leftover `.tmp` files after `delete()`.** `delete()` globs for `climate_advisor_state_*.tmp` and removes all matches.
6. **File permissions are set.** After `os.replace()`, `save()` calls `os.chmod(str(self._path), 0o600)` (guarded by `hasattr(os, "chmod")`, so it's a no-op on platforms without it, e.g. Windows) — satisfying the CLAUDE.md security rule requiring `0o600` on persisted state files. Fixed in Issue #384 (v0.4.53); this section previously described a gap that no longer exists — verified directly against `state.py` during the 2026-09-16 documentation staleness audit.

## Disclosure Path

← Tier 1 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md) (see "Integration Version" and "Coordinator State Listeners" sections)
→ Tier 3 specs: none yet authored — candidates: `StatePersistence.save` atomic write contract, `StatePersistence.load` version guard contract
↔ Siblings: [docs/learning-engine.md (not yet written)] — `LearningEngine`/`LearningState` is the parallel persistence subsystem for the learning DB
