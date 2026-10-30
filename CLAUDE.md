# CLAUDE.md

Home Assistant custom integration (`daycarewebwatch`) for DaycareWebWatch classroom cameras, distributed
through HACS as a custom repository. It talks to the private API used by the official mobile app.
User-facing behaviour is documented in `README.md`; keep it in sync with code changes.

## Commands

All tooling runs through [uv](https://docs.astral.sh/uv/) (Python 3.14, pinned in `.python-version`; HA test
dependencies pinned in `uv.lock`). These are exactly what CI (`.github/workflows/tests.yml`) runs:

```bash
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict, covers custom_components/daycarewebwatch only
uv run pytest --cov --cov-report=term-missing   # coverage must stay >= 95 %
```

`av`/`PyTurboJPEG` need the system `libturbojpeg` (Ubuntu 24.04 package name: `libturbojpeg`).

`scripts/smoke.py` hits the **real** service with a real parent account. Never run it yourself; ask the user to
run it (`! uv run python -I scripts/smoke.py ...`) when live verification is needed.

## Layout

`custom_components/daycarewebwatch/`:

| File | Role |
|---|---|
| `api.py` | Async HTTP client (`DaycareWebWatchClient`), error types (`InvalidAuth`, `CannotConnect`, `UnsupportedApiVersion`, `ViewingClosed`), `Camera` model, per-endpoint request counters. No HA imports. |
| `coordinator.py` | `CameraListCoordinator` (every 6 h) and `ViewingStatusCoordinator` (adaptive interval: open / closed-in-window / closed / error backoff). Also caches recent stream URLs (`remember_url` / `recent_url`) and tracks streaming cameras. `DaycareWebWatchData` is the entry's `runtime_data`. |
| `camera.py` | `DaycareCamera`: HLS stream source, token reuse, stream repair (callback + 60 s check), placeholder / last-frame stills, optional WebRTC. |
| `binary_sensor.py` | "Viewing available" sensor with `reason` / `code` attributes. |
| `schedule.py` | `ViewingWindow` (opening hours from options) used only to pace polling. |
| `config_flow.py` | User, reauth, reconfigure (same account only) and options flows. `VERSION = 1`. |
| `entity.py`, `helpers.py` | Shared base entity (device per camera), dynamic addition of new cameras. |
| `diagnostics.py` | Redacts credentials, device id, children's names, stream URLs. |
| `const.py` | All intervals and option keys, with comments explaining the values. |
| `strings.json` + `translations/en.json` | Must stay **identical**; edit both. |
| `quality_scale.yaml` | HA quality-scale self-assessment; update when a rule's status changes. |
| `manifest.json` | Domain, `version` (release version), `dependencies: ["stream"]`, no `requirements`. |

Repo root: `hacs.json` (minimum HA version), `CHANGELOG.md`, `.github/workflows/` (`tests.yml`,
`validate.yml` = HACS action + hassfest, `release.yml` = tag/manifest version check).

## Design rules

- **Minimize requests to the service.** Every `viewcamera` call issues a stream token; the service is not ours
  and the app does far fewer calls. Reuse URLs younger than `TOKEN_REUSE_MAX_AGE`, only request new tokens
  when a stream actually fails, never start streams for stills, don't status-check watched cameras. Any change
  that adds requests needs a clear reason and a test asserting how many client calls are made (`call_count` on `mock_client` methods).
- Closed cameras are a normal state (`is_on` false, entity still available), not an error. Unavailable means
  the service is unreachable.
- Server refusals are classified by message, not by error number (live numbers differ from the app's).
- **Privacy:** never log tokens, stream URLs, passwords or children's names; keep diagnostics redaction
  complete. Never add real footage or images to the repo, tests or issues.
- Follow current HA patterns: `entry.runtime_data`, `DataUpdateCoordinator`, `has_entity_name`, translated
  errors/repairs, `async_update_reload_and_abort` for reauth/reconfigure.
- Code style: ruff (line length 100, docstrings required outside tests/scripts), strict mypy, short comments
  that explain *why*.

## Tests

- `tests/conftest.py`: `config_entry` (`MockConfigEntry`) and `mock_client` (patches the client class in both
  `__init__` and `config_flow`). Use `freezegun` / `async_fire_time_changed` for time-based behaviour.
- After a flow that reloads the entry (reauth, reconfigure, options), `await hass.async_block_till_done()`
  before the test ends, or CI fails with "Lingering timer" from the camera's stream-check interval.
- New behaviour needs tests; keep coverage at 95 % or above.

## Release process

Versions follow [SemVer](https://semver.org) and AwesomeVersion (what HA and HACS parse): `MAJOR.MINOR.PATCH`,
pre-releases as `X.Y.ZbN`. HACS installs from **GitHub releases**, so a release is only visible to users once
it is published there. Bump MAJOR for breaking changes (removed/renamed entities or options, config entry
migrations users must know about, higher minimum HA version), MINOR for features, PATCH for fixes.

1. **Prepare on `main`** (CI green on the commit you will release):
   - Set the version in `custom_components/daycarewebwatch/manifest.json` (`version`, required by HA for custom
     integrations) and in `pyproject.toml` (`project.version`); they must match.
   - In `CHANGELOG.md`, rename `## Unreleased` to `## X.Y.Z` and list user-visible changes, with
     **Breaking changes** first and upgrade steps for each.
   - If the minimum Home Assistant version changed, update `hacs.json` (`homeassistant`), the README
     prerequisites and the pinned `pytest-homeassistant-custom-component` together.
   - If stored config entry data or options changed shape, bump `VERSION`/`MINOR_VERSION` in the config flow and
     add `async_migrate_entry` with tests. Never break existing entries silently.
   - Run the full command set above; commit as `Release X.Y.Z` and push. Wait for Tests and Validate to pass.
2. **Publish** a GitHub release whose tag is `vX.Y.Z` on that commit:

   ```bash
   gh release create vX.Y.Z --target main --title "vX.Y.Z" --notes-file <changelog section>
   # pre-release (only shown in HACS to users who enable beta versions):
   gh release create vX.Y.ZbN --target main --title "vX.Y.ZbN" --prerelease --notes-file ...
   ```

   The release notes are what HACS shows in its update dialog: copy the changelog section, breaking changes
   first. Do not upload zip assets (`hacs.json` has no `zip_release`; HACS downloads the repository content).
3. **Verify:** the `Release` workflow checks that the tag (minus `v`) equals the manifest version. If it fails,
   delete the release and tag, fix the version on `main`, and publish again; do this only before users can
   have installed it. Once a release has been out, never move or reuse its tag: fix forward with a new patch
   release.
4. Start a new `## Unreleased` section in `CHANGELOG.md` for the next changes.

Publishing a release is outward-facing: only do steps 2–3 when the user asks for a release.
