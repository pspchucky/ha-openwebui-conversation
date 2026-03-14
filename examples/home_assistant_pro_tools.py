"""
title: Home Assistant Pro Tools
author: Pastry Labs
author_url: https://home.pastrylabs.cloud
funding_url: https://github.com/open-webui
version: 1.4.0
type: python
"""

import json
import os
import re
import time
from difflib import get_close_matches
from typing import Any, Dict, List, Tuple

import requests
from pydantic import BaseModel, Field, validator


ALLOWED_NAME_MAP = {
    "Middle bedroom": "light.michaels_old_room",
    "Michael's Old Room": "light.michaels_old_room",
    "Michaels old room": "light.michaels_old_room",
    "Michael old bedroom": "light.michaels_old_room",
    "Michael bedroom": "light.michaels_old_room",
    "Box fan": "switch.fan_outlet_2",
    "Bed fan": "switch.fan_outlet_2",
    "Bedroom fan": "switch.fan_outlet_2",
    "Kitchen": "light.sink",
    "Dining Room": "light.dining_table",
    "Living Room": "light.couch_light",
    "Hallway": "light.hallway",
    "Bug zapper": "switch.bug_zapper",
}
ALLOWED_ENTITIES = set(ALLOWED_NAME_MAP.values())

COLOR_NAME_MAP = {
    "red": [255, 0, 0],
    "green": [0, 255, 0],
    "blue": [0, 0, 255],
    "yellow": [255, 255, 0],
    "purple": [128, 0, 128],
    "magenta": [255, 0, 255],
    "cyan": [0, 255, 255],
    "orange": [255, 165, 0],
    "pink": [255, 192, 203],
    "white": [255, 255, 255],
    "warmwhite": [255, 244, 229],
    "coolwhite": [204, 229, 255],
}


def _slugify(value: str) -> str:
    value = (value or "").strip().casefold()
    value = re.sub(r"[_\-\s]+", " ", value)
    value = re.sub(r"[^\w\s\.]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value


def _parse_bool(value: str, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _normalize_targets(name: str, names: Any, names_csv: str) -> List[str]:
    targets: List[str] = []
    if isinstance(name, str) and name.strip():
        targets.append(name.strip())
    if names is not None:
        if isinstance(names, list):
            targets += [str(x).strip() for x in names if str(x).strip()]
        elif isinstance(names, str) and names.strip():
            targets += [part.strip() for part in names.split(",") if part.strip()]
    if isinstance(names_csv, str) and names_csv.strip():
        targets += [part.strip() for part in names_csv.split(",") if part.strip()]

    seen = set()
    deduped: List[str] = []
    for target in targets:
        if target not in seen:
            seen.add(target)
            deduped.append(target)
    return deduped


class Tools:
    class Valves(BaseModel):
        BASE_URL: str = Field(default_factory=lambda: os.getenv("HA_BASE_URL", ""))
        AUTH_TOKEN: str = Field(default_factory=lambda: os.getenv("HA_TOKEN", ""))
        VERIFY_SSL: bool = Field(
            default_factory=lambda: _parse_bool(os.getenv("HA_VERIFY_SSL", "true"), True)
        )
        TIMEOUT: float = Field(default_factory=lambda: float(os.getenv("HA_TIMEOUT", "10")))
        DRY_RUN: bool = Field(
            default_factory=lambda: _parse_bool(os.getenv("HA_DRY_RUN", "false"), False)
        )

        @validator("BASE_URL")
        def _strip_trailing_slash(cls, value: str) -> str:
            return value[:-1] if value.endswith("/") else value

    def __init__(self):
        self.valves = self.Valves()
        self._session = requests.Session()

    def _assert_config(self):
        if not self.valves.BASE_URL or not self.valves.AUTH_TOKEN:
            raise RuntimeError("Set HA_BASE_URL and HA_TOKEN environment variables.")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.valves.AUTH_TOKEN}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.valves.BASE_URL}{path if path.startswith('/') else '/' + path}"

    def _request(
        self, method: str, path: str, *, json_body: Dict[str, Any] | None = None
    ) -> requests.Response:
        self._assert_config()
        response = self._session.request(
            method.upper(),
            self._url(path),
            headers=self._headers(),
            json=json_body,
            timeout=self.valves.TIMEOUT,
            verify=self.valves.VERIFY_SSL,
        )
        if not (200 <= response.status_code < 300):
            try:
                detail = response.json()
            except Exception:
                detail = response.text[:500]
            raise RuntimeError(f"HTTP {response.status_code} {method} {path} :: {detail}")
        return response

    def _resolve_multiple(
        self, names_or_ids: List[str], domain: str = ""
    ) -> Tuple[List[str], List[str]]:
        allowed_names = list(ALLOWED_NAME_MAP.keys())
        normalized_map = {_slugify(name): entity_id for name, entity_id in ALLOWED_NAME_MAP.items()}

        found: List[str] = []
        misses: List[str] = []

        for raw_name in names_or_ids:
            if not raw_name:
                continue
            normalized = _slugify(raw_name)

            if normalized in ("all", "everything", "everyone"):
                misses.append(raw_name)
                continue

            if "." in raw_name:
                entity_id = raw_name.strip()
                if entity_id in ALLOWED_ENTITIES and (
                    not domain or entity_id.startswith(f"{domain}.")
                ):
                    found.append(entity_id)
                else:
                    misses.append(raw_name)
                continue

            entity_id = normalized_map.get(normalized)
            if entity_id and (not domain or entity_id.startswith(f"{domain}.")):
                found.append(entity_id)
                continue

            close = get_close_matches(raw_name, allowed_names, n=1, cutoff=0.85)
            if close:
                entity_id = ALLOWED_NAME_MAP[close[0]]
                if not domain or entity_id.startswith(f"{domain}."):
                    found.append(entity_id)
                    continue

            misses.append(raw_name)

        seen = set()
        deduped: List[str] = []
        for entity_id in found:
            if entity_id not in seen:
                seen.add(entity_id)
                deduped.append(entity_id)
        return deduped, misses

    def set_config(
        self,
        base_url: str = "",
        token: str = "",
        verify_ssl: str = "",
        dry_run: str = "",
    ) -> str:
        if base_url:
            self.valves.BASE_URL = base_url.rstrip("/")
        if token:
            self.valves.AUTH_TOKEN = token
        if verify_ssl != "":
            self.valves.VERIFY_SSL = _parse_bool(verify_ssl, self.valves.VERIFY_SSL)
        if dry_run != "":
            self.valves.DRY_RUN = _parse_bool(dry_run, self.valves.DRY_RUN)
        return "Configuration updated."

    def list_entities(self, domain: str = "") -> str:
        lines = []
        for name, entity_id in sorted(ALLOWED_NAME_MAP.items()):
            if domain and not entity_id.startswith(f"{domain}."):
                continue
            lines.append(f"{name} -> {entity_id}")
        return "\n".join(lines) or "No entities found."

    def health_check(self) -> str:
        try:
            config = self._request("GET", "/api/config").json()
            return (
                f"Connected to {config.get('location_name', 'Home Assistant')} "
                f"(version {config.get('version', 'unknown')})."
            )
        except Exception as exc:
            return f"Health check failed: {exc}"

    def call_service_raw(
        self, domain: str, service: str, entities_csv: str, data_json: str = ""
    ) -> str:
        try:
            names = [value.strip() for value in (entities_csv or "").split(",") if value.strip()]
            entity_ids, misses = self._resolve_multiple(names, domain=domain.strip())
            if not entity_ids:
                return f"Not found: {entities_csv}"
            payload: Dict[str, Any] = {"entity_id": entity_ids}
            if data_json:
                extra = json.loads(data_json)
                if not isinstance(extra, dict):
                    return "data_json must be a JSON object."
                payload.update(extra)
            if self.valves.DRY_RUN:
                return f"[DRY RUN] {domain}/{service} {entity_ids} {payload}"
            self._request("POST", f"/api/services/{domain}/{service}", json_body=payload)
            message = f"Called {domain}/{service} for {len(entity_ids)} target(s)"
            if misses:
                message += f". Not found: {', '.join(misses)}"
            return message
        except Exception as exc:
            return f"Error: {exc}"

    def control_lights(
        self,
        name: str = "",
        names: Any = None,
        names_csv: str = "",
        state: str = "",
        brightness: int = -1,
        brightness_pct: int = -1,
        rgb: Any = "",
        color_temp_mireds: int = -1,
        transition: float = 0.0,
    ) -> str:
        try:
            targets = _normalize_targets(name, names, names_csv)
            if not targets:
                return "Please provide a light name, names, or names_csv."

            state = (state or "").strip().lower()
            if not state and (int(brightness) > 0 or int(brightness_pct) > 0):
                state = "on"
            if state not in ("on", "off"):
                state = "on"

            payload: Dict[str, Any] = {}
            if state == "on":
                if isinstance(brightness_pct, int) and brightness_pct > 0:
                    payload["brightness_pct"] = max(1, min(100, brightness_pct))
                elif isinstance(brightness, int) and brightness > 0:
                    if brightness <= 100:
                        payload["brightness_pct"] = brightness
                    else:
                        payload["brightness"] = max(1, min(255, brightness))

                rgb_triplet = None
                if isinstance(rgb, list) and len(rgb) == 3:
                    rgb_triplet = [int(max(0, min(255, part))) for part in rgb]
                elif isinstance(rgb, str) and rgb.strip():
                    try:
                        parsed = json.loads(rgb)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, list) and len(parsed) == 3:
                        rgb_triplet = [int(max(0, min(255, part))) for part in parsed]
                    else:
                        color_name = rgb.strip().lower().replace(" ", "")
                        rgb_triplet = COLOR_NAME_MAP.get(color_name)
                if rgb_triplet:
                    payload["rgb_color"] = rgb_triplet

                if int(color_temp_mireds) > 0:
                    payload["color_temp"] = int(color_temp_mireds)
                if float(transition) > 0:
                    payload["transition"] = float(transition)

            entity_ids, misses = self._resolve_multiple(targets, domain="light")
            if not entity_ids:
                return f"Not found: {', '.join(misses)}"

            service_payload = {"entity_id": entity_ids}
            service_payload.update(payload)

            if not self.valves.DRY_RUN:
                self._request("POST", f"/api/services/light/turn_{state}", json_body=service_payload)
            message = f"Lights {state}: {len(entity_ids)}"
            if misses:
                message += f". Not found: {', '.join(misses)}"
            return message
        except Exception as exc:
            return f"Error: {exc}"

    def control_switches(
        self, name: str = "", names: Any = None, names_csv: str = "", state: str = "on"
    ) -> str:
        try:
            targets = _normalize_targets(name, names, names_csv)
            if not targets:
                return "Please provide a switch name, names, or names_csv."
            state = (state or "on").strip().lower()
            if state not in ("on", "off"):
                state = "on"
            entity_ids, misses = self._resolve_multiple(targets, domain="switch")
            if not entity_ids:
                return f"Not found: {', '.join(misses)}"
            if not self.valves.DRY_RUN:
                self._request(
                    "POST",
                    f"/api/services/switch/turn_{state}",
                    json_body={"entity_id": entity_ids},
                )
            message = f"Switches {state}: {len(entity_ids)}"
            if misses:
                message += f". Not found: {', '.join(misses)}"
            return message
        except Exception as exc:
            return f"Error: {exc}"

    def media_player_command(
        self,
        name: str = "",
        names: Any = None,
        names_csv: str = "",
        action: str = "",
        volume_level: float = -1.0,
    ) -> str:
        try:
            targets = _normalize_targets(name, names, names_csv)
            if not targets:
                return "Please provide a media player name, names, or names_csv."

            service_map = {
                "play": "media_play",
                "pause": "media_pause",
                "stop": "media_stop",
                "mute": "volume_mute",
                "unmute": "volume_mute",
                "volume_set": "volume_set",
            }
            action = (action or "").strip().lower()
            service = service_map.get(action)
            if not service:
                return "Invalid action. Use play, pause, stop, mute, unmute, or volume_set."

            entity_ids, misses = self._resolve_multiple(targets, domain="media_player")
            if not entity_ids:
                return f"Not found: {', '.join(misses)}"

            payload: Dict[str, Any] = {"entity_id": entity_ids}
            if action in ("mute", "unmute"):
                payload["is_volume_muted"] = action == "mute"
            if action == "volume_set":
                payload["volume_level"] = max(0.0, min(1.0, float(volume_level)))

            if not self.valves.DRY_RUN:
                self._request("POST", f"/api/services/media_player/{service}", json_body=payload)
            message = f"Media {action}: {len(entity_ids)}"
            if misses:
                message += f". Not found: {', '.join(misses)}"
            return message
        except Exception as exc:
            return f"Error: {exc}"

    def climate_set_temperature(
        self,
        name: str = "",
        names: Any = None,
        names_csv: str = "",
        temperature_c: float = 0.0,
        hvac_mode: str = "",
    ) -> str:
        try:
            targets = _normalize_targets(name, names, names_csv)
            if not targets:
                return "Please provide a climate entity name, names, or names_csv."
            entity_ids, misses = self._resolve_multiple(targets, domain="climate")
            if not entity_ids:
                return f"Not found: {', '.join(misses)}"
            payload: Dict[str, Any] = {
                "entity_id": entity_ids,
                "temperature": float(temperature_c),
            }
            if hvac_mode.strip():
                payload["hvac_mode"] = hvac_mode.strip()
            if not self.valves.DRY_RUN:
                self._request("POST", "/api/services/climate/set_temperature", json_body=payload)
            message = f"Set temperature for {len(entity_ids)} device(s)"
            if misses:
                message += f". Not found: {', '.join(misses)}"
            return message
        except Exception as exc:
            return f"Error: {exc}"

    def wait(self, seconds: int = 0) -> str:
        try:
            seconds = max(0, min(60, int(seconds or 0)))
            if not self.valves.DRY_RUN:
                time.sleep(seconds)
            return f"Waited {seconds} second(s)"
        except Exception as exc:
            return f"Error: {exc}"

    def light_on_then_off_after_delay(
        self,
        name: str = "",
        names: Any = None,
        names_csv: str = "",
        seconds: int = 5,
        brightness: int = -1,
        brightness_pct: int = -1,
        rgb: Any = "",
        color_temp_mireds: int = -1,
        transition: float = 0.0,
    ) -> str:
        try:
            targets = _normalize_targets(name, names, names_csv)
            if not targets:
                return "Please provide a light name, names, or names_csv."

            first = self.control_lights(
                names=targets,
                state="on",
                brightness=brightness,
                brightness_pct=brightness_pct,
                rgb=rgb,
                color_temp_mireds=color_temp_mireds,
                transition=transition,
            )
            if first.startswith(("Error:", "Not found:", "Please provide")):
                return first

            waited = self.wait(seconds)
            if waited.startswith("Error:"):
                return waited

            second = self.control_lights(
                names=targets,
                state="off",
                transition=transition,
            )
            if second.startswith(("Error:", "Not found:")):
                return second

            return f"Lights on, waited {max(0, min(600, int(seconds or 0)))} second(s), then lights off."
        except Exception as exc:
            return f"Error: {exc}"

    def controlDevice(self, entityID: str, domain: str, service: str) -> str:
        try:
            if entityID not in ALLOWED_ENTITIES:
                return f"Not allowed: {entityID}"
            if not self.valves.DRY_RUN:
                self._request(
                    "POST",
                    f"/api/services/{domain}/{service}",
                    json_body={"entity_id": [entityID]},
                )
            return "Successfully changed the device"
        except Exception as exc:
            return f"Error: {exc}"
