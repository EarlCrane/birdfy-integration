"""HTTP proxy views for Birdfy HLS streams."""
from __future__ import annotations

import re

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

def register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(BirdfyM3U8ProxyView(hass))
    hass.http.register_view(BirdfySegmentProxyView)


def _segment_sort_key(url: str) -> tuple[int, int]:
    m = re.search(r"slice_\d+_(\d+)_(\d+)\.ts", url)
    if m:
        return int(m.group(2)), int(m.group(1))  # (gop, fragment_index)
    return (0, 0)


def _build_m3u8_from_segments(segments: list[tuple[str, float]]) -> str:
    """Build a valid HLS playlist from (url, duration) pairs."""
    sorted_segments = sorted(segments, key=lambda x: _segment_sort_key(x[0]))
    max_duration = max((d for _, d in sorted_segments), default=2.0)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{int(max_duration) + 1}",
    ]
    prev_gop: int | None = None
    for url, duration in sorted_segments:
        m = re.search(r"slice_\d+_(\d+)_(\d+)\.ts", url)
        gop = int(m.group(2)) if m else 0
        if prev_gop is not None and gop != prev_gop:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(url)
        prev_gop = gop
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


def _fix_proxied_m3u8(content: str) -> str:
    """Normalize a Netvue M3U8: preserve EXTINF durations and rebuild a clean playlist."""
    content = content.replace(" #", "\n#")
    segments: list[tuple[str, float]] = []
    pending_duration: float = 2.0
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line[8:].split(",")[0])
            except ValueError:
                pending_duration = 2.0
        elif line and not line.startswith("#"):
            segments.append((line, pending_duration))
            pending_duration = 2.0
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


class BirdfySegmentProxyView(HomeAssistantView):
    """Proxies a single .ts segment from S3 (fallback for old-style URLs)."""

    url = "/api/birdfy/segment/{encoded_url}"
    name = "api:birdfy:segment"
    requires_auth = False

    async def get(self, request: web.Request, encoded_url: str) -> web.Response:
        import urllib.parse
        url = urllib.parse.unquote(urllib.parse.unquote(encoded_url))

        if not url.startswith("https://nvs-eu-central-1-videomotion.s3"):
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
