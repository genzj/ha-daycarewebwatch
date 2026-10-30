# Changelog

## 1.0.0

- New option **Use WebRTC (go2rtc)**, off by default: go2rtc 1.9.x crashes on these streams (affecting all
  cameras using it) and each WebRTC start requests another stream link. Cameras use HA's HLS player.
- Debug logging explains every request (endpoint, camera, result, request number) and every token/stream
  decision; tokens are never logged. Diagnostics include request counts per endpoint.
- The options dialog shows the time zone the opening hours use (Home Assistant's).
- README: exact steps for live view, debug logging, Preload camera stream; WebRTC and time zone sections.
- Check viewing status by opening the camera (`viewcamera`); `checkcamerapermission` only works for an
  already-open stream.
- Handle server refusals by their message: the live server's error numbers differ from the app's.
  Unknown refusals are retried once after logging in again.
- Cameras refused "for today" are not asked again until the next opening time.
- Stream links are only renewed when a stream fails, instead of every 150 s. Failures are repaired
  immediately (stream state callback), at most once per 30 s.
- Links younger than 4 minutes are reused (tokens open connections for ~5 minutes), so a status check
  followed by a viewer costs one request. Watched cameras are not status-checked.
- New viewers of an idle stream with an expired link get a fresh stream instead of a 403.
- Camera pictures no longer start streams: placeholder image, or the last frame from a watched stream.
- The *Viewing available* sensor shows the service's `reason` message and `code`.
- Fix: use the login response's `SessionId` (not `Token`) for authenticated calls.

## 0.1.0

- Initial release: camera entities with live HLS view, "Viewing available" binary sensors,
  automatic stream-token refresh, adaptive polling for closed hours, reauth/reconfigure,
  viewing-window options, diagnostics.
