# Changelog

## [4.1.1](https://github.com/gerke-homelab/openproject-mcp-server/compare/v4.1.0...v4.1.1) (2026-08-04)


### Bug Fixes

* **api:** translate row offset to page number for pagination ([#59](https://github.com/gerke-homelab/openproject-mcp-server/issues/59)) ([f14bbe1](https://github.com/gerke-homelab/openproject-mcp-server/commit/f14bbe1c77fbdb508266df75022849e19744e3df))
* **deps:** bump pygments past the GUID-regex ReDoS ([#87](https://github.com/gerke-homelab/openproject-mcp-server/issues/87)) ([4bb43f9](https://github.com/gerke-homelab/openproject-mcp-server/commit/4bb43f9557966d5780b03bebd964fcc65712bc43))

## [4.1.0](https://github.com/gerket/openproject-mcp-server/compare/v4.0.0...v4.1.0) (2026-06-27)


### Features

* **tools:** add get_work_package tool ([#930](https://github.com/gerket/openproject-mcp-server/issues/930)) ([#52](https://github.com/gerket/openproject-mcp-server/issues/52)) ([9a8379f](https://github.com/gerket/openproject-mcp-server/commit/9a8379f2a1c210db3c172fffc43025eef02d897b))
* **tools:** add list_capabilities and describe_tool discovery tools ([#53](https://github.com/gerket/openproject-mcp-server/issues/53)) ([24be632](https://github.com/gerket/openproject-mcp-server/commit/24be6328aaf3ce93ef0312a5c2b5b7196e097bc9))


### Bug Fixes

* **api:** wrap bare-string long-text custom fields using form schema ([#50](https://github.com/gerket/openproject-mcp-server/issues/50)) ([5359309](https://github.com/gerket/openproject-mcp-server/commit/5359309617afc5aef54fc9f0250cb78672da5e85))

## [4.0.0](https://github.com/gerket/openproject-mcp-server/compare/v3.0.0...v4.0.0) (2026-06-20)


### ⚠ BREAKING CHANGES

* **server:** fastmcp 3.0 removed the include_tags and exclude_tags constructor arguments, changed the mcp.tool decorator to return plain functions instead of FunctionTool wrappers, and replaced the get_tools method with list_tools. All call sites and test patterns were migrated to the new API.

### Features

* **ci:** require scope in all PR titles, force 4.0.0 release ([#31](https://github.com/gerket/openproject-mcp-server/issues/31)) ([eef1ca2](https://github.com/gerket/openproject-mcp-server/commit/eef1ca2966c262fbc7f478890222b6e5c3c42f93))
* **server:** upgrade to fastmcp 3.x ([#22](https://github.com/gerket/openproject-mcp-server/issues/22)) ([46329d8](https://github.com/gerket/openproject-mcp-server/commit/46329d8d9064b35bc5854051f9ab3bca8179749f))


### Bug Fixes

* **ci:** upgrade release-please-action to v5 (node24), drop invalid package-name input ([#28](https://github.com/gerket/openproject-mcp-server/issues/28)) ([70a85d0](https://github.com/gerket/openproject-mcp-server/commit/70a85d0467b404471a0c718d7bbfe294f9466eab))
* **release:** add packages section with include-component-in-tag false ([#34](https://github.com/gerket/openproject-mcp-server/issues/34)) ([2a22cda](https://github.com/gerket/openproject-mcp-server/commit/2a22cdaf0d3564ad01fb2396f2a6a94b14309ce8))
* **release:** set manifest version to 3.0.0 to match last tag ([#33](https://github.com/gerket/openproject-mcp-server/issues/33)) ([d080c15](https://github.com/gerket/openproject-mcp-server/commit/d080c15ce02c1b65b666478692ebf4bd08a6db81))
* **release:** set PR title pattern to use release scope ([#48](https://github.com/gerket/openproject-mcp-server/issues/48)) ([1269ad7](https://github.com/gerket/openproject-mcp-server/commit/1269ad71f1caa40ad6d3f05e03659975602f09b9))


### Documentation

* **docs:** document BREAKING CHANGE footer convention for major releases ([#46](https://github.com/gerket/openproject-mcp-server/issues/46)) ([f30b3dd](https://github.com/gerket/openproject-mcp-server/commit/f30b3dd629e338ee37d71d4323819b195f1e4ac3))
* **docs:** never enable auto-merge when opening a PR ([#35](https://github.com/gerket/openproject-mcp-server/issues/35)) ([02384b0](https://github.com/gerket/openproject-mcp-server/commit/02384b04eadd4d45c7911de69c67a8e8424948e6))

## [3.0.0] — 2026-06-20

Complete rewrite from a single-file monolith to a modular 25-module server.

### Added
- **120 tools** across 25 modules covering the full actionable OpenProject v3 API
- **Tag-based filtering** — `read`/`write`, category, `admin`, `core`/`situational`, composite tags; control exposed tools with `OPENPROJECT_MCP_INCLUDE_TAGS` / `OPENPROJECT_MCP_EXCLUDE_TAGS`
- **`admin` permission tag** — hide admin-only tools from non-admin tokens with `OPENPROJECT_MCP_EXCLUDE_TAGS=admin`
- **`storage` category tag** — hide file-server tools on instances without OneDrive/Nextcloud
- Phase E: user management (`create_user`, `update_user`), principals, placeholder users, preferences
- Phase F: documents (list/get/update), file storages, file links (list/get/create/delete), project-storage links
- Phase G: work package categories, views
- Phase D: custom actions (list/get/execute), version lifecycle (update/delete)
- Phase C: saved queries (full CRUD + star/unstar), env-var tag filtering
- Phase B: watchers, activity editing, available assignees, reminders
- **192 unit tests** + **79 live integration tests** with a one-time setup script (`scripts/setup_test_project.py`)
- Integration test bot user (`mcp-test-bot`) for notification testing
- `api_paths` session fixture — skips tests with spec-based messages for absent endpoints
- Release workflow (`.github/workflows/release.yml`)

### Changed
- Modular architecture: `src/tools/` (25 modules) replacing single-file implementation
- `uv` replaces pip; `uv run openproject-mcp` is the canonical run command
- Tests restructured: `tests/unit/` (192 tests) + `tests/integration/` (79 tests)
- `get_time_entry_activities` now uses form schema (no `/time_entries/activities` endpoint in v3)
- `get_memberships` filter fixed: `user` → `principal`
- `update_activity` payload fixed: `{"comment": "string"}` not `{"comment": {"raw": "..."}}`
- Server startup: import errors propagate immediately — no silent partial loading

### Removed
- Single-file `openproject-mcp.legacy.py` (135KB)
- Non-functional entry point scripts (`openproject-mcp-http.py`, `-sse.py`, `-fastmcp.py`)
- `requirements.txt` — `pyproject.toml` is the source of truth
- Cost entry tools (`list_cost_types`, `list_cost_entries`, create/update/delete) — endpoints not in core v3 API; replaced with `list_budgets` + `get_budget`

### Fixed
- `conftest.py` default project slug `"infrastructure"` → `"mcp-test"`
- Stale `--ignore=test_live_integration.py` in `pyproject.toml` addopts
- `check_permissions` now tagged `core`/`core-read`
- `assign_work_package`, `unassign_work_package` now tagged `situational`
- `mark_notification_read`, `generate_this_week_report`, `generate_last_week_report` now tagged `situational`

## [2.0.0] — 2026-06-19

Initial modular rewrite — CI, pytest, pre-commit, packaging, tag system introduced.

## [1.0.0] — 2026-06-16

Original single-file implementation (`openproject-mcp.legacy.py`).
