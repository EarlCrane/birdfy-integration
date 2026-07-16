"""Birdfy camera — thumbnail of last detection."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BirdfyCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BirdfyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BirdfyThumbnailCamera(coordinator, entry)])


class BirdfyThumbnailCamera(CoordinatorEntity[BirdfyCoordinator], Camera):
    """Shows the thumbnail image of the last Birdfy detection event."""

    _attr_supported_features = CameraEntityFeature(0)
    _attr_is_streaming = False

    def __init__(self, coordinator: BirdfyCoordinator, entry: ConfigEntry) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{entry.entry_id}_thumbnail"
        self._attr_name = "Birdfy Netvue Thumbnail"
        self._cached_image: bytes | None = None
        self._cached_url: str = ""

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        url = self.coordinator.image_url
        if not url:
            return self._cached_image
        if url == self._cached_url and self._cached_image:
            return self._cached_image
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    if r.status == 200:
                        self._cached_image = await r.read()
                        self._cached_url = url
                        return self._cached_image
        except Exception as e:
            _LOGGER.warning("Failed to fetch Birdfy thumbnail: %s", e)
        return self._cached_image
