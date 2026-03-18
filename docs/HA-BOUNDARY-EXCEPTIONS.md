# Home Assistant Boundary Exceptions

This file tracks approved exceptions where Climate Advisor interacts with Home Assistant outside its own integration directory (`custom_components/climate_advisor/`). Each exception should be periodically reviewed and resolved when possible.

## Active Exceptions

### 1. Learning Engine Database File

- **Date**: 2026-03-18
- **What**: The learning engine writes `climate_advisor_learning.json` to the HA config root (`/config/climate_advisor_learning.json`)
- **Why**: HA's config directory is the standard persistent storage location for custom integrations that need to store state across restarts. The learning engine needs a 90-day rolling window of daily observations and user feedback to generate suggestions.
- **Risk**: Low. The file is a single JSON file owned entirely by Climate Advisor. It does not modify any existing HA files. If the file is deleted, the learning engine resets gracefully with no impact to HA.
- **Resolution plan**: Migrate to HA's `hass.helpers.storage.Store` API, which manages storage under `.storage/` with proper versioning and atomic writes. This is the HA-recommended approach for integration-managed data. Target: v0.2.0.
- **Status**: Active — awaiting migration to `Store` API

---

## Resolved Exceptions

_None yet._

---

## Review Schedule

Review all active exceptions quarterly or before each minor version release. For each exception, ask:
1. Is this still necessary?
2. Has HA added a better-supported way to accomplish this?
3. Can this be moved inside the integration's own scope?
4. What is the current risk level?
