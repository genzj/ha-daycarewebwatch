"""Manual end-to-end check against the real DaycareWebWatch service.

Usage:
    python -I scripts/smoke.py --email you@example.com [--camera ID] [--probe]
                               [--check-only] [--lifetime MINUTES]
                               [--proxy socks5://[user:pass@]host:port]

Logs in, lists cameras, opens the selected cameras and prints the results.
Stream tokens are bound to the caller's public IP, so use --proxy to pin all
traffic (API calls, HLS requests, ffprobe/ffmpeg) to one egress IP.
The instance id is stored in ~/.config/daycarewebwatch-smoke so repeated runs
look like the same device.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urljoin, urlsplit
import uuid

import aiohttp
from aiohttp_socks import ProxyConnector
from python_socks.async_.asyncio import Proxy

API_PATH = Path(__file__).resolve().parent.parent / "custom_components/daycarewebwatch/api.py"
spec = importlib.util.spec_from_file_location("dcww_api", API_PATH)
assert spec and spec.loader
api = importlib.util.module_from_spec(spec)
sys.modules["dcww_api"] = api
spec.loader.exec_module(api)

STATE = Path.home() / ".config" / "daycarewebwatch-smoke"
EXO_UA = "DaycareWebWatch/6.0.10 (Linux;Android 14) AndroidXMedia3/1.8.0"
SECRET_KEYS = {"VideoToken", "SessionId", "Token"}
Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]
TOKEN_RE = re.compile(r"\?[^\s'\"]+")


class Net:
    """How this run reaches the internet (direct or through a SOCKS proxy)."""

    proxy: str | None = None  # socks5://... for Python requests
    bridge: str | None = None  # http://127.0.0.1:port for ffmpeg/ffprobe


def new_session() -> aiohttp.ClientSession:
    """Create a client session that honours --proxy."""
    if Net.proxy:
        return aiohttp.ClientSession(connector=ProxyConnector.from_url(Net.proxy, rdns=True))
    return aiohttp.ClientSession()


def ff_env() -> dict[str, str]:
    """Environment for ffmpeg/ffprobe: route http and https via the local bridge."""
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        env.pop(key, None)
    if Net.bridge:
        # ffmpeg reads http_proxy for both http:// and https:// (via CONNECT).
        env["http_proxy"] = Net.bridge
    return env


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.close()


class SocksBridge:
    """Loopback-only HTTP proxy that forwards every connection through SOCKS5.

    ffmpeg cannot use SOCKS proxies, but it can use an HTTP proxy. This bridge
    listens on 127.0.0.1 (random port) for the lifetime of the script only.
    """

    def __init__(self, socks_url: str) -> None:
        self._socks_url = socks_url
        self._server: asyncio.Server | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _open(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        sock = await Proxy.from_url(self._socks_url, rdns=True).connect(host, port, timeout=30)
        return await asyncio.open_connection(sock=sock)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            request_line, *headers = head.split("\r\n")
            method, target, version = request_line.split(" ", 2)
            if method == "CONNECT":  # https: tunnel bytes as-is
                host, port = target.rsplit(":", 1)
                up_reader, up_writer = await self._open(host, int(port))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:  # plain http: absolute-URI request -> origin-form
                url = urlsplit(target)
                up_reader, up_writer = await self._open(url.hostname or "", url.port or 80)
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                kept = [h for h in headers if not h.lower().startswith(("proxy-", "connection:"))]
                kept.insert(0, "Connection: close")
                up_writer.write(
                    f"{method} {path} {version}\r\n{'\r\n'.join(kept)}".encode("latin-1")
                )
                await up_writer.drain()
            await asyncio.gather(
                _pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True
            )
        except Exception as err:
            print(f"      [bridge] connection failed: {TOKEN_RE.sub('?<token>', str(err))}")
        finally:
            with contextlib.suppress(Exception):
                writer.close()


def instance_id() -> str:
    if STATE.exists():
        return STATE.read_text().strip()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    STATE.write_text(value)
    return value


def hide(url: str) -> str:
    """Hide query strings (tokens) in printed URLs."""
    return url.split("?", 1)[0] + ("?<token>" if "?" in url else "")


async def ffprobe(url: str) -> None:
    if not shutil.which("ffprobe"):
        print("    ffprobe not installed; skipping")
        return
    for ua in (None, EXO_UA):
        cmd = ["ffprobe", "-v", "info", "-hide_banner"]
        if ua:
            cmd += ["-user_agent", ua]
        cmd += ["-show_entries", "stream=codec_name,width,height", "-of", "compact", url]
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=ff_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            print("    ffprobe timed out")
            continue
        label = "app User-Agent" if ua else "default User-Agent"
        if proc.returncode == 0:
            streams = sorted(set(out.decode().split()))
            print(f"    ffprobe OK ({label}): {' '.join(streams)}")
            return
        lines = [TOKEN_RE.sub("?<token>", ln) for ln in err.decode().splitlines()]
        print(f"    ffprobe failed ({label}):")
        for ln in [ln for ln in lines if "Opening" in ln or "rror" in ln or "404" in ln][:12]:
            print(f"      {ln}")


async def fetch(session: aiohttp.ClientSession, url: str, ua: str | None) -> tuple[int, str]:
    headers = {"User-Agent": ua} if ua else {}
    async with session.get(url, headers=headers, allow_redirects=True) as resp:
        body = await resp.read()
        is_text = b"#EXT" in body[:20]
        text = body[:2000].decode("utf-8", "replace") if is_text else f"<{len(body)} bytes>"
        return resp.status, text


async def trace_hls(session: aiohttp.ClientSession, url: str) -> None:
    """Walk master playlist -> media playlist -> first segment, like a player."""
    for ua in (None, EXO_UA):
        print(f"    HLS trace (User-Agent: {ua or 'aiohttp default'})")
        level_url = url
        for level in ("playlist", "chunklist", "segment"):
            status, text = await fetch(session, level_url, ua)
            print(f"      {level}: HTTP {status} {hide(level_url)}")
            if status != 200 or not text.startswith("#EXT"):
                break
            for line in text.splitlines()[:15]:
                print(f"        | {hide(line) if '?' in line else line}")
            uris = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
            if not uris:
                break
            level_url = urljoin(level_url, uris[0])


async def check_ip_stability(samples: int = 6) -> None:
    """Warn if the public IP changes between connections (multi-IP NAT/VPN).

    Stream tokens and the ApiKey are tied to the caller's public IP, so an
    unstable egress IP makes requests fail randomly with 404 / Access Denied.
    """
    seen: list[str] = []
    for _ in range(samples):
        # A fresh session per sample forces a new connection each time.
        async with new_session() as probe, probe.get(api.IP_URL) as resp:
            seen.append((await resp.text()).strip())
    distinct = sorted(set(seen))
    via = " (via proxy)" if Net.proxy else ""
    if len(distinct) == 1:
        print(f"Public IP stable{via}: {distinct[0]}")
    else:
        print(f"WARNING: public IP changes between connections{via}: {', '.join(distinct)}")
        print("  Tokens are IP-bound, so streams will fail randomly on this network.")
        print("  Use --proxy socks5://host:port, or a network with a single public IP.")


async def reopen_status(url: str) -> str:
    """Open playlist -> chunklist on fresh connections, like a new viewer would."""
    async with new_session() as fresh:
        async with fresh.get(url) as resp:
            if resp.status != 200:
                return f"playlist HTTP {resp.status}"
            text = await resp.text()
        uris = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        if not uris:
            return "playlist has no variants"
        async with fresh.get(urljoin(url, uris[0])) as resp:
            return f"playlist 200, chunklist HTTP {resp.status}"


async def measure_lifetime(url: str, minutes: int) -> None:
    """Answer two questions about the token.

    1. Does a continuous player keep playing past the token's age?  (ffmpeg)
    2. How long can the same URL be re-opened by a new connection?   (reopen)
    """
    print(f"    Lifetime test for {minutes} min (Ctrl+C to stop early)")
    player = None
    if shutil.which("ffmpeg"):
        player = await asyncio.create_subprocess_exec(
            *["ffmpeg", "-v", "error", "-i", url, "-t", str(minutes * 60)],
            *["-c", "copy", "-f", "null", "-"],
            env=ff_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        print("      ffmpeg not installed; only testing re-open")
    loop = asyncio.get_running_loop()
    start = loop.time()
    while (elapsed := loop.time() - start) < minutes * 60:
        reopen = await reopen_status(url)
        playing = "n/a"
        if player is not None:
            playing = "playing" if player.returncode is None else f"STOPPED rc={player.returncode}"
        print(f"      t+{elapsed / 60:5.1f} min  re-open: {reopen:32}  continuous: {playing}")
        await asyncio.sleep(300)
    if player is not None:
        _, err = await player.communicate()
        tail = TOKEN_RE.sub("?<token>", err.decode()[-400:]).strip()
        print(f"      continuous player finished rc={player.returncode} {tail}")


async def raw(client: object, path: str, data: dict[str, str]) -> dict[str, object]:
    """Call an endpoint and return the raw JSON, never raising on API errors."""
    try:
        return await client._raw_post(path, data, with_session=True)  # type: ignore[attr-defined]
    except api.DaycareWebWatchError as err:
        return {"Status": "exception", "Message": str(err)}


def summarize(result: dict[str, object]) -> str:
    """Show status, error and message plus the other keys (secrets hidden)."""
    shown = {k: ("<hidden>" if k in SECRET_KEYS else v) for k, v in result.items()}
    return str(shown)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="prompted for if omitted")
    parser.add_argument("--probe", action="store_true", help="trace HLS and run ffprobe")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="only call checkcamerapermission (no viewcamera) to test it as a cheap status probe",
    )
    parser.add_argument(
        "--lifetime",
        type=int,
        metavar="MINUTES",
        help="measure token lifetime: play continuously and re-open the same URL every 5 min",
    )
    parser.add_argument(
        "--camera",
        type=int,
        action="append",
        metavar="ID",
        help="only open these camera ids (repeatable); others are listed but not touched",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("DCWW_PROXY"),
        metavar="URL",
        help="SOCKS proxy for all traffic, e.g. socks5://127.0.0.1:1080 "
        "(default: $DCWW_PROXY). DNS is resolved by the proxy.",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace, password: str) -> None:
    async with new_session() as session:
        await check_ip_stability()
        client = api.DaycareWebWatchClient(session, args.email, password, instance_id())
        await client.async_login()
        print("Login OK")
        cameras = await client.async_get_cameras()
        print(f"{len(cameras)} camera(s)")
        for cam in cameras:
            print(f"- {cam.id} {cam.name!r} @ {cam.daycare_name!r} server={cam.server}")
            if args.camera and cam.id not in args.camera:
                print("    skipped (not selected with --camera)")
                continue
            if args.check_only:
                check = await raw(client, "checkcamerapermission", {"CameraId": str(cam.id)})
                print("    checkcamerapermission (no viewcamera):", summarize(check))
                continue
            # Same order as the app: open the camera first, then the periodic check.
            view = await raw(client, "viewcamera", {"CameraId": str(cam.id), "LowBandwidth": "0"})
            check = await raw(client, "checkcamerapermission", {"CameraId": str(cam.id)})
            print("    viewcamera:           ", summarize(view))
            print("    checkcamerapermission:", summarize(check))
            if view.get("Status") == 1 and view.get("VideoToken"):
                url = api.build_stream_url(cam, str(view["VideoToken"]))
                print("    stream URL (token hidden):", url.split("?", 1)[0])
                if args.probe:
                    await trace_hls(session, url)
                    await ffprobe(url)
                if args.lifetime:
                    await measure_lifetime(url, args.lifetime)


async def main() -> int:
    args = parse_args()
    password = args.password or getpass.getpass("Password: ")
    bridge = None
    if args.proxy:
        if not args.proxy.startswith(("socks5://", "socks5h://", "socks4://")):
            print("--proxy must be a socks5:// (or socks4://) URL")
            return 2
        Net.proxy = args.proxy.replace("socks5h://", "socks5://", 1)
        bridge = SocksBridge(Net.proxy)
        Net.bridge = await bridge.start()
        print(f"Using SOCKS proxy {urlsplit(Net.proxy).hostname}:{urlsplit(Net.proxy).port}")
    try:
        await run(args, password)
    finally:
        if bridge is not None:
            await bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
