"""HTTP proxy views for Birdfy HLS streams."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_SEGMENT_DURATION = 2.0


def _is_allowed_media_url(url: str) -> bool:
    """Return whether a URL points at a known Netvue media origin."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower()
    if hostname.startswith("cdn-nvs-") and hostname.endswith(
        ("-videomotion.nvts.co", "-motioncapture.nvts.co")
    ):
        return True

    return bool(
        re.fullmatch(
            r"nvs-[a-z0-9-]+-(?:video)?motion\.s3"
            r"(?:[.-][a-z0-9-]+)?\.amazonaws\.com",
            hostname,
        )
    )


def register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(BirdfyM3U8ProxyView(hass))
    hass.http.register_view(BirdfyMp4ProxyView(hass))
    hass.http.register_view(BirdfySegmentProxyView)


def _segment_sort_key(url: str) -> tuple[int, int]:
    m = re.search(r"slice_\d+_(\d+)_(\d+)\.ts", url)
    if m:
        return int(m.group(2)), int(m.group(1))  # (gop, fragment_index)
    return (0, 0)


def _build_m3u8_from_segments(segments: list[str]) -> str:
    """Build a valid HLS playlist from segment URLs."""
    sorted_segments = sorted(segments, key=_segment_sort_key)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{int(_SEGMENT_DURATION) + 1}",
    ]
    prev_gop: int | None = None
    for url in sorted_segments:
        m = re.search(r"slice_\d+_(\d+)_(\d+)\.ts", url)
        if not m:
            continue
        gop = int(m.group(2))
        if prev_gop is not None and gop != prev_gop:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{_SEGMENT_DURATION:.3f},")
        lines.append(url)
        prev_gop = gop
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


def _fix_proxied_m3u8(content: str) -> str:
    """Normalize a Netvue M3U8: extract segment URLs and rebuild a clean playlist."""
    content = content.replace(" #", "\n#")
    segments = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            segments.append(line)
    if not segments:
        return content
    return _build_m3u8_from_segments(segments)


class BirdfyM3U8ProxyView(HomeAssistantView):
    """Serves an HLS playlist for a given alarm_id."""

    url = "/api/birdfy/m3u8/{alarm_id}"
    name = "api:birdfy:m3u8"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, alarm_id: str) -> web.Response:
        coordinator = None
        for c in self.hass.data.get(DOMAIN, {}).values():
            coordinator = c
            break
        if coordinator is None:
            return web.Response(status=503, text="Birdfy not ready")

        record_url = coordinator.record_url_cache.get(alarm_id)
        if not record_url and coordinator.data:
            for ev in coordinator.data.get("recent_events", []):
                if ev["alarm_id"] == alarm_id:
                    record_url = ev["record_url"]
                    break

        if not record_url:
            return web.Response(status=404, text="Event not found")

        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(record_url) as r:
                    if r.status != 200:
                        return web.Response(status=r.status, text="Upstream error")
                    content = await r.text()
        except Exception as e:
            return web.Response(status=502, text=str(e))

        result = _fix_proxied_m3u8(content)
        return web.Response(
            text=result,
            content_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"},
        )


class BirdfyMp4ProxyView(HomeAssistantView):
    """Transcodes HLS segments to fMP4 via ffmpeg and streams to browser."""

    url = "/api/birdfy/mp4/{alarm_id}"
    name = "api:birdfy:mp4"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, alarm_id: str) -> web.StreamResponse:
        coordinator = None
        for c in self.hass.data.get(DOMAIN, {}).values():
            coordinator = c
            break
        if coordinator is None:
            return web.Response(status=503, text="Birdfy not ready")

        record_url = coordinator.record_url_cache.get(alarm_id)
        if not record_url and coordinator.data:
            for ev in coordinator.data.get("recent_events", []):
                if ev["alarm_id"] == alarm_id:
                    record_url = ev["record_url"]
                    break

        if not record_url:
            record_url = await coordinator.fetch_fresh_record_url(alarm_id)

        if not record_url:
            return web.Response(status=404, text="Event not found")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
        os.close(tmp_fd)
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y",
                    "-f", "hls",
                    "-i", record_url,
                    "-c", "copy",
                    "-movflags", "faststart",
                    "-f", "mp4",
                    tmp_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                _LOGGER.error("ffmpeg not found — cannot transcode HLS to MP4")
                return web.Response(status=500, text="ffmpeg not available")

            _, stderr_data = await proc.communicate()

            if proc.returncode != 0 or os.path.getsize(tmp_path) == 0:
                _LOGGER.error("ffmpeg failed for %s: %s", alarm_id, stderr_data.decode(errors="replace"))
                return web.Response(status=502, text="ffmpeg failed")

            with open(tmp_path, "rb") as f:
                mp4_data = f.read()
        finally:
            os.unlink(tmp_path)

        return web.Response(
            body=mp4_data,
            content_type="video/mp4",
            headers={"Access-Control-Allow-Origin": "*"},
        )


class BirdfySegmentProxyView(HomeAssistantView):
    """Proxies a single .ts segment from S3 (fallback for old-style URLs)."""

    url = "/api/birdfy/segment/{encoded_url}"
    name = "api:birdfy:segment"
    requires_auth = False

    async def get(self, request: web.Request, encoded_url: str) -> web.Response:
        url = unquote(unquote(encoded_url))

        if not _is_allowed_media_url(url):
            return web.Response(status=403, text="Forbidden")

        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    data = await r.read()
                    return web.Response(
                        body=data,
                        content_type="video/mp2t",
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
        except Exception as e:
            return web.Response(status=502, text=str(e))
