# DaycareWebWatch for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![GitHub Release](https://img.shields.io/github/v/release/genzj/ha-daycarewebwatch)](https://github.com/genzj/ha-daycarewebwatch/releases)
[![Validate](https://github.com/genzj/ha-daycarewebwatch/actions/workflows/validate.yml/badge.svg)](https://github.com/genzj/ha-daycarewebwatch/actions/workflows/validate.yml)
[![Tests](https://github.com/genzj/ha-daycarewebwatch/actions/workflows/tests.yml/badge.svg)](https://github.com/genzj/ha-daycarewebwatch/actions/workflows/tests.yml)

Watch your child's [DaycareWebWatch](https://www.daycarewebwatch.com) classroom cameras in Home Assistant.

DaycareWebWatch is a cloud service that childcare centres use to share live classroom video with parents.
Access is protected by a login, and the video links expire after a short time, so the cameras cannot be
added to Home Assistant as a plain generic or RTSP camera. This integration signs in with your parent
account, finds the cameras you are allowed to see, and keeps the video link fresh for you.

> [!IMPORTANT]
> This is an unofficial, community-made integration. It is not affiliated with or endorsed by DaycareWebWatch.
> It uses the same private API as the official mobile app, which may change without notice.
> Use it for your own account only and respect your childcare centre's privacy rules.

## Features

- **Camera entity** per classroom camera, with live view in dashboards and on cast devices (HLS).
- **"Viewing available" binary sensor** per camera: on while the daycare is open and the camera is on.
- Expiring stream links are handled automatically: a new link is requested only when a stream needs one.
- **Closed hours are handled gracefully**: at night and on weekends the camera shows as *off* (not as an
  error), and the integration checks for reopening much less often.
- Cameras added to or removed from your account appear or become removable automatically.
- UI setup, re-authentication, reconfiguration, options, and diagnostics download.

## Supported devices

| Supported | Not supported |
|---|---|
| Live view of every camera your parent account can access, across all your children and daycares | Low-bandwidth (MJPEG) mode, recorded videos (VoD), photos, chat, newsletters, daily reports |
| Email + password accounts | Google or Facebook sign-in (set a password for your account in the app first) |

## Prerequisites

- A DaycareWebWatch parent account whose access has been approved by your childcare centre.
  Check that you can watch the cameras in the official app first.
- Home Assistant 2026.10.0 or newer.
- [HACS](https://hacs.xyz) (for the recommended installation method).

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS**.
2. Open the menu (⋮) in the top right and select **Custom repositories**.
3. Add `https://github.com/genzj/ha-daycarewebwatch` with type **Integration**.
4. Search for **DaycareWebWatch**, open it and select **Download**.
5. Restart Home Assistant.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=genzj&repository=ha-daycarewebwatch&category=integration)

### Manual

1. Download the latest release from the [releases page](https://github.com/genzj/ha-daycarewebwatch/releases).
2. Copy the `custom_components/daycarewebwatch` folder into the `custom_components` folder of your
   Home Assistant configuration directory (create it if needed).
3. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & services → Add integration** and search for **DaycareWebWatch**.
2. Enter your account details:

   | Field | Description |
   |---|---|
   | Email | The email address of your DaycareWebWatch parent account. |
   | Password | The password of your DaycareWebWatch parent account. |

The integration signs in before saving, so a wrong password is reported right away. Each account can be
added once. Add a second entry for a second parent account.

### Options

Open **Settings → Devices & services → DaycareWebWatch → Configure** to set when your daycare is normally open:

| Option | Default | Description |
|---|---|---|
| Opens at | 06:30 | Local time when cameras usually become viewable. |
| Closes at | 18:30 | Local time after which cameras are usually off. |
| Open on | Monday–Friday | Days of the week the daycare is open. |

These settings only control **how often** the integration checks a closed camera. They never block viewing:
if the daycare opens early, the camera turns on as soon as the next check sees it.

## Entities

Each camera is a device named after the camera (for example *Toddler Room*), placed in an area named
after the daycare.

| Entity | Description | Attributes |
|---|---|---|
| `camera.<daycare>_<camera>` | Live view. Shows *off* while viewing is not possible. | `children`, `daycare` |
| `binary_sensor.<daycare>_<camera>_viewing_available` | On while the camera can be watched. | `reason`: the service's explanation when off (e.g. *You do not have access to this camera today*), `code`: its error code |

## How data is updated

This is a cloud polling integration. It keeps traffic low, especially outside opening hours:

| What | When |
|---|---|
| Camera list | Every 6 hours. |
| Viewing status, camera viewable | Every hour. |
| Viewing status, closed during opening hours | Every 10 minutes, to catch the opening quickly. |
| Viewing status, "no access to this camera today" | Not checked again until the next day's opening time. |
| Viewing status, closed at night / weekends | One check at the next opening time, at most every 3 hours. |
| Viewing status, service unreachable | Backs off from 5 up to 60 minutes. |
| Stream link | Requested when you open a camera, and again only if a running stream fails. Idle streams are closed. |

The service has no "is this camera open?" query, so a status check opens the camera the same way the app does
(this requests a stream link but doesn't download video). The schedule above keeps those requests rare. When
the whole daycare is closed, only one camera per daycare is checked.

Your account can list cameras that you are not allowed to watch (for example other classrooms). They appear
with *Viewing available* off and the service's reason, and are checked at most once a day.

## Examples

### Dashboard card that only shows during opening hours

```yaml
type: conditional
conditions:
  - condition: state
    entity: binary_sensor.sunny_daycare_toddler_room_viewing_available
    state: "on"
card:
  type: picture-entity
  entity: camera.sunny_daycare_toddler_room
  camera_view: live
```

### Cast the classroom to a display when the daycare opens

```yaml
automation:
  - alias: "Show classroom on kitchen display"
    triggers:
      - trigger: state
        entity_id: binary_sensor.sunny_daycare_toddler_room_viewing_available
        to: "on"
    conditions:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
    actions:
      - action: camera.play_stream
        target:
          entity_id: camera.sunny_daycare_toddler_room
        data:
          media_player: media_player.kitchen_display
```

### Use cases

- A live-view tile on a wall tablet that appears only while the daycare is open.
- Cast the classroom to a TV at lunch time.
- Get notified when the camera comes on in the morning.

## Known limitations

- Outside opening hours the camera is shown as **off**. Reopening is detected within about 10 minutes during
  your configured opening hours, or at the first check at the configured opening time.
- No snapshot endpoint exists, so the camera picture comes from the live stream and only appears while
  viewing is available.
- Low-bandwidth mode, recorded videos and the other app features are not supported.
- The service ties sessions to your public IP address. If your IP changes, the integration signs in again
  automatically.
- Unofficial API: a DaycareWebWatch app update can break the integration until it is updated.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Invalid authentication" during setup | Check that you can sign in to the official app with the same email and password. Google/Facebook-only accounts need a password. |
| A repair says re-authentication is required | Your password changed. Select **Reconfigure** in the notification and enter the new password. |
| Camera shows *off* during the day | Check the `reason` attribute of the *Viewing available* sensor: it shows the service's message, e.g. no access to this camera today (ask your centre), or the camera is not currently available (turned off by the centre). |
| Camera is *unavailable* | Home Assistant cannot reach DaycareWebWatch. It recovers on its own; check your internet connection. |
| Live view fails with 404 / random "Access Denied" | Stream tokens are bound to your public IP. Networks that spread connections over several public IPs (corporate networks, some VPNs, load-balanced NAT) break them. Run Home Assistant on a connection with a single public IP; `scripts/smoke.py` prints a warning when it detects this. |
| Live view does not start | Make sure the `stream` integration works (it is part of `default_config`). Download diagnostics and check the logs. |
| Repair: "API version not supported" | The service changed. Update the integration through HACS. |

To enable debug logging, add the following to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.daycarewebwatch: debug
```

Or use **Enable debug logging** on the integration page.

### Diagnostics

Go to **Settings → Devices & services → DaycareWebWatch → ⋮ → Download diagnostics**. Email, password,
device id, children's names and stream links are removed from the file. Attach it when opening an issue.

## Re-authentication and reconfiguration

- If the password stops working, Home Assistant shows a *re-authentication required* notification.
  Enter the new password there.
- To change the password at any time, select **⋮ → Reconfigure** on the integration entry.

## Removal

1. Go to **Settings → Devices & services → DaycareWebWatch**.
2. Select **⋮ → Delete** on the entry.
3. Optionally remove the integration in **HACS → DaycareWebWatch → ⋮ → Remove**, then restart Home Assistant.

The integration stores nothing outside its config entry.

## Contributing

Issues and pull requests are welcome at <https://github.com/genzj/ha-daycarewebwatch/issues>.

Development uses [uv](https://docs.astral.sh/uv/). It installs the pinned Python and Home Assistant test
dependencies from `uv.lock`:

```bash
uv sync                    # create .venv with Python 3.14 + dev dependencies
uv run pytest --cov        # tests, >95 % coverage enforced
uv run ruff check . && uv run ruff format --check .
uv run mypy                # strict
```

### Testing against the real service

`scripts/smoke.py` checks the API with your own account. It logs in, lists your cameras, and opens only the
cameras you select. The password is prompted for if `--password` is omitted.

```bash
# Open one camera, trace the HLS playlists and run ffprobe on the stream
uv run python -I scripts/smoke.py --email you@example.com --camera 1234 --probe
```

| Option | Purpose |
|---|---|
| `--camera ID` | Only open this camera (repeatable). Other cameras are listed but never requested, so cameras you can see but can't access don't collect "access denied" errors. |
| `--probe` | Walk the HLS playlist → chunklist → segment and run `ffprobe` (tokens are hidden in the output). |
| `--check-only` | Only call `checkcamerapermission`, without opening the camera. |
| `--lifetime MINUTES` | Keep an `ffmpeg` player running and re-open the same URL every 5 minutes, to measure how long a stream token stays valid. |
| `--proxy URL` | Send **all** traffic through a SOCKS proxy (see below). Defaults to `$DCWW_PROXY`. |

`ffprobe`/`ffmpeg` are needed for `--probe` and `--lifetime` (`brew install ffmpeg`, `apt install ffmpeg`).

#### Testing through a SOCKS5 proxy

Stream tokens and API keys are bound to your public IP. If your network uses several public IPs (the script
prints a `WARNING: public IP changes between connections` line), test through a SOCKS5 proxy with a single exit IP:

```bash
uv run python -I scripts/smoke.py --email you@example.com --camera 1234 --probe \
    --proxy socks5://127.0.0.1:7070

# or set it once for the shell
export DCWW_PROXY=socks5://127.0.0.1:7070
uv run python -I scripts/smoke.py --email you@example.com --camera 1234 --lifetime 60
```

- Supported schemes: `socks5://`, `socks5h://` and `socks4://`, with optional `user:pass@`. Host names are always
  resolved by the proxy.
- The API calls, the HLS trace, and `ffprobe`/`ffmpeg` all use the proxy. ffmpeg can't speak SOCKS, so the script
  starts a temporary HTTP-to-SOCKS bridge on `127.0.0.1` (random port). The bridge is only reachable from your
  machine and stops when the script exits.
- The first line of output confirms the exit IP, e.g. `Public IP stable (via proxy): 203.0.113.7`.

The proxy option is for this test script only. The integration itself connects directly, so run Home Assistant
on a network with a single public IP.

## Disclaimer

DaycareWebWatch is a trademark of its respective owner. This project is not affiliated with it. Video of
children is sensitive: keep your Home Assistant instance secure and don't share camera access with others.

## License

[MIT](LICENSE)
