"""HTTP proxy views for Birdfy HLS streams."""
from __future__ import annotations

import logging

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Approximate segment duration in seconds (Netvue segments are ~2s each)
_SEGMENT_DURATION = 2.0


def register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(BirdfyM3U8ProxyView(hass))
    hass.http.register_view(BirdfySegmentProxyView)


def _segment_sort_key(url: str) -> tuple[int, int]:
    import re
    m = re.search(r"slice_(\d+)_(\d+)_\d+\.ts", url)
    if m:
        return int(m.group(1)), int(m.group(2))
    return (0, 0)


def _build_m3u8_from_segments(segments: list[str]) -> str:
    """Build a valid HLS playlist, inserting EXT-X-DISCONTINUITY at every GOP/fragment gap."""
    import re
    sorted_segments = sorted(segments, key=_segment_sort_key)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{int(_SEGMENT_DURATION) + 1}",
    ]
    prev_gop: int | None = None
    prev_frag: int | None = None
    for url in sorted_segments:
        m = re.search(r"slice_(\d+)_(\d+)_\d+\.ts", url)
        gop = int(m.group(1)) if m else None
        frag = int(m.group(2)) if m else None
        discontinuous = (
            prev_gop is not None and (
                gop != prev_gop or (frag is not None and prev_frag is not None and frag != prev_frag + 1)
            )
        )
        if discontinuous:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{_SEGMENT_DURATION:.3f},")
        lines.append(url)
        prev_gop = gop
        prev_frag = frag
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


def _fix_proxied_m3u8(content: str) -> str:
    """Normalize a Netvue M3U8 fetched from the signed recordUrl."""
    content = content.replace(" #", "\n#")
    lines = []
    has_endlist = False
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            line = f"#EXTINF:{_SEGMENT_DURATION:.3f},"
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            line = f"#EXT-X-TARGETDURATION:{int(_SEGMENT_DURATION) + 1}"
        elif line == "#EXT-X-ENDLIST":
            has_endlist = True
        lines.append(line)
    if not has_endlist:
        lines.append("#EXT-X-ENDLIST")
    if lines and lines[0] == "#EXTM3U" and "#EXT-X-INDEPENDENT-SEGMENTS" not in lines:
        lines.insert(1, "#EXT-X-INDEPENDENT-SEGMENTS")
    return "\n".join(lines)


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

        # Fetch the signed M3U8 URL from cache
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
