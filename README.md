# Birdfy Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant custom integration for [Birdfy](https://www.birdfy.com/) smart bird feeder cameras. Browse and play SD card recordings directly from the HA Media Browser.

## Features

- **Media Browser**: browse recordings by day and play clips directly in HA
- **Sensor**: last detection event with timestamp and attributes
- **Camera**: thumbnail of the last detection
- Polls the Netvue API every 5 minutes
- No local hardware required — uses the Netvue cloud API

## Installation via HACS

1. In Home Assistant, go to **HACS → Integrations**
2. Click the three-dot menu → **Custom repositories**
3. Add `https://github.com/sebsst/birdfy-integration` with category **Integration**
4. Install **Birdfy Integration**
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration** → search for **Birdfy**
7. Enter your Netvue/Birdfy account email and password

## Entities

| Entity | Description |
|--------|-------------|
| `sensor.birdfy_last_event` | Label and attributes of the last detection |
| `camera.birdfy_thumbnail` | Thumbnail image of the last detection |

## Media Browser

Open the **Media Browser** in the HA sidebar → **Birdfy** → select a day → play a clip.

## Notes

- The `label` field is always `unknown` if your camera model does not return AI classification
- SD card recordings are stored as HLS streams on Netvue S3 servers, valid for 1 hour after fetch
- For live RTSP streaming, see [birdfy-rtsp](https://github.com/sebsst/birdfy-rtsp)
