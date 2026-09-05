# Changelog

## 0.1.1

- Fix stale readings for resumed tasks whose rollout filenames include a segment UUID suffix.
- Resolve continuation history using validated metadata references, with explicit errors for ambiguous or incomplete chains.
- Share the bounded scan budget across inherited segments and respect recorded byte boundaries, including compaction detection across files.
- Add synthetic continuation regressions and a resumed-task MCP stdio integration test.

## 0.1.0

- Add a standard-library CLI for explicitly selected local Codex threads.
- Add a read-only MCP stdio tool using the official Python SDK.
- Report last-request counters, recorded effective window, estimated remaining context, and event age.
- Withhold estimates after recognized compaction until a new usage record arrives.
- Bound log reads, validate session identity, and expose structured errors without conversation contents.
