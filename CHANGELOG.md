# Changelog

## 0.1.0

- Add a standard-library CLI for explicitly selected local Codex threads.
- Add a read-only MCP stdio tool using the official Python SDK.
- Report last-request counters, recorded effective window, estimated remaining context, and event age.
- Withhold estimates after recognized compaction until a new usage record arrives.
- Bound log reads, validate session identity, and expose structured errors without conversation contents.
