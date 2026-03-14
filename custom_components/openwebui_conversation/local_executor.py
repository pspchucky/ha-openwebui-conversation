"""Local Home Assistant tool execution for OpenWebUI responses."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from .const import LOGGER
from .helpers import get_exposed_entities


@dataclass
class ExecutedStep:
    """A locally executed action."""

    kind: str
    names: list[str]
    state: str | None = None
    seconds: int | None = None


def _normalize_tool_name(name: str | None) -> str:
    if not isinstance(name, str):
        return ""
    name = name.strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def _normalize_name_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        if "," in text:
            return [part.strip() for part in text.split(",") if part.strip()]
        return [text]
    return []


def _parse_json_from_text(content: str | None) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def extract_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return tool calls from native or prompt-based response shapes."""
    choices = response.get("choices") or []
    if not choices:
        return []
    message = (choices[0] or {}).get("message") or {}
    native_calls = message.get("tool_calls") or []
    normalized_calls: list[dict[str, Any]] = []
    if isinstance(native_calls, list) and native_calls:
        for tool_call in native_calls:
            fn = (tool_call or {}).get("function") or {}
            name = _normalize_tool_name(fn.get("name"))
            if not name:
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    parsed = _parse_json_from_text(args)
                    args = parsed if isinstance(parsed, dict) else {}
            if not isinstance(args, dict):
                args = {}
            normalized_calls.append({"name": name, "parameters": args})
        if normalized_calls:
            return normalized_calls

    parsed_content = _parse_json_from_text(message.get("content"))
    if not isinstance(parsed_content, dict):
        return []
    prompt_calls = parsed_content.get("tool_calls")
    if not isinstance(prompt_calls, list):
        return []
    for tool_call in prompt_calls:
        if not isinstance(tool_call, dict):
            continue
        name = _normalize_tool_name(tool_call.get("name"))
        if not name:
            continue
        params = tool_call.get("parameters")
        if isinstance(params, str):
            parsed = _parse_json_from_text(params)
            params = parsed if isinstance(parsed, dict) else {}
        if not isinstance(params, dict):
            params = {}
        normalized_calls.append({"name": name, "parameters": params})
    return normalized_calls


def _entity_index(hass: HomeAssistant) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for entity in get_exposed_entities(hass):
        keys = {entity["entity_id"].lower(), entity["name"].lower()}
        for alias in entity.get("aliases", []):
            if alias:
                keys.add(str(alias).lower())
        if entity["name"]:
            keys.add(f"{entity['name'].lower()} light")
            keys.add(f"{entity['name'].lower()} lights")
            keys.add(f"{entity['name'].lower()} switch")
            keys.add(f"{entity['name'].lower()} switches")
        for key in keys:
            index.setdefault(key, []).append(entity)
    return index


def _resolve_entities(
    hass: HomeAssistant, names: list[str], expected_domain: str | None = None
) -> tuple[list[str], list[str]]:
    index = _entity_index(hass)
    resolved_ids: list[str] = []
    resolved_names: list[str] = []
    for raw_name in names:
        key = raw_name.strip().lower()
        candidates = index.get(key, [])
        if expected_domain:
            candidates = [
                entity
                for entity in candidates
                if entity["entity_id"].split(".", 1)[0] == expected_domain
            ]
        if not candidates:
            continue
        entity = candidates[0]
        entity_id = entity["entity_id"]
        if entity_id not in resolved_ids:
            resolved_ids.append(entity_id)
            resolved_names.append(entity["name"])
    return resolved_ids, resolved_names


async def _call_service(
    hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]
) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


async def _execute_control_lights(
    hass: HomeAssistant, parameters: dict[str, Any]
) -> ExecutedStep | None:
    names = _normalize_name_list(parameters.get("names"))
    if not names:
        names = _normalize_name_list(parameters.get("name"))
    entity_ids, resolved_names = _resolve_entities(hass, names, "light")
    if not entity_ids:
        return None
    state = str(parameters.get("state", "")).strip().lower()
    data: dict[str, Any] = {"entity_id": entity_ids}
    service = "turn_on"
    if state == "off":
        service = "turn_off"
    else:
        brightness_pct = parameters.get("brightness_pct")
        if isinstance(brightness_pct, str) and brightness_pct.strip().isdigit():
            brightness_pct = int(brightness_pct.strip())
        if isinstance(brightness_pct, int) and 1 <= brightness_pct <= 100:
            data["brightness_pct"] = brightness_pct
        rgb = parameters.get("rgb")
        if isinstance(rgb, str):
            try:
                rgb = json.loads(rgb)
            except Exception:
                rgb = None
        if isinstance(rgb, list) and len(rgb) == 3:
            try:
                data["rgb_color"] = [int(part) for part in rgb]
            except Exception:
                pass
    await _call_service(hass, "light", service, data)
    return ExecutedStep("lights", resolved_names or names, "off" if service == "turn_off" else "on")


async def _execute_control_switches(
    hass: HomeAssistant, parameters: dict[str, Any]
) -> ExecutedStep | None:
    names = _normalize_name_list(parameters.get("names"))
    if not names:
        names = _normalize_name_list(parameters.get("name"))
    entity_ids, resolved_names = _resolve_entities(hass, names, "switch")
    if not entity_ids:
        return None
    state = str(parameters.get("state", "")).strip().lower()
    service = "turn_off" if state == "off" else "turn_on"
    await _call_service(hass, "switch", service, {"entity_id": entity_ids})
    return ExecutedStep(
        "switches", resolved_names or names, "off" if service == "turn_off" else "on"
    )


async def _execute_media_player_command(
    hass: HomeAssistant, parameters: dict[str, Any]
) -> ExecutedStep | None:
    names = _normalize_name_list(parameters.get("names"))
    if not names:
        names = _normalize_name_list(parameters.get("name"))
    entity_ids, resolved_names = _resolve_entities(hass, names, "media_player")
    if not entity_ids:
        return None
    action = str(parameters.get("action", "")).strip().lower()
    service_map = {
        "play": "media_play",
        "pause": "media_pause",
        "stop": "media_stop",
        "mute": "volume_mute",
        "unmute": "volume_mute",
        "volume_set": "volume_set",
    }
    service = service_map.get(action)
    if not service:
        return None
    data: dict[str, Any] = {"entity_id": entity_ids}
    if action in ("mute", "unmute"):
        data["is_volume_muted"] = action == "mute"
    if action == "volume_set":
        volume_level = parameters.get("volume_level")
        if isinstance(volume_level, str):
            try:
                volume_level = float(volume_level.strip())
            except Exception:
                volume_level = None
        if isinstance(volume_level, (int, float)):
            data["volume_level"] = float(volume_level)
    await _call_service(hass, "media_player", service, data)
    return ExecutedStep("media_player", resolved_names or names, action)


async def _execute_climate_set_temperature(
    hass: HomeAssistant, parameters: dict[str, Any]
) -> ExecutedStep | None:
    names = _normalize_name_list(parameters.get("names"))
    if not names:
        names = _normalize_name_list(parameters.get("name"))
    entity_ids, resolved_names = _resolve_entities(hass, names, "climate")
    if not entity_ids:
        return None
    temperature_c = parameters.get("temperature_c")
    if isinstance(temperature_c, str):
        try:
            temperature_c = float(temperature_c.strip())
        except Exception:
            temperature_c = None
    if not isinstance(temperature_c, (int, float)):
        return None
    data: dict[str, Any] = {
        "entity_id": entity_ids,
        "temperature": float(temperature_c),
    }
    hvac_mode = parameters.get("hvac_mode")
    if isinstance(hvac_mode, str) and hvac_mode.strip():
        data["hvac_mode"] = hvac_mode.strip()
    await _call_service(hass, "climate", "set_temperature", data)
    return ExecutedStep("climate", resolved_names or names, f"{float(temperature_c):g}C")


async def _execute_wait(parameters: dict[str, Any]) -> ExecutedStep | None:
    seconds = parameters.get("seconds", parameters.get("duration", 0))
    if isinstance(seconds, str):
        try:
            seconds = int(float(seconds.strip()))
        except Exception:
            seconds = 0
    if not isinstance(seconds, (int, float)):
        return None
    seconds = max(0, min(int(seconds), 60))
    await asyncio.sleep(seconds)
    return ExecutedStep("wait", [], seconds=seconds)


async def execute_tool_calls(
    hass: HomeAssistant, tool_calls: list[dict[str, Any]]
) -> list[ExecutedStep]:
    """Execute supported tool calls in order."""
    steps: list[ExecutedStep] = []
    for tool_call in tool_calls:
        name = _normalize_tool_name(tool_call.get("name"))
        parameters = tool_call.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        step: ExecutedStep | None = None
        if name == "control_lights":
            step = await _execute_control_lights(hass, parameters)
        elif name == "control_switches":
            step = await _execute_control_switches(hass, parameters)
        elif name == "media_player_command":
            step = await _execute_media_player_command(hass, parameters)
        elif name == "climate_set_temperature":
            step = await _execute_climate_set_temperature(hass, parameters)
        elif name == "wait":
            step = await _execute_wait(parameters)
        else:
            LOGGER.debug("Ignoring unsupported local tool call: %s", name)
        if step is not None:
            steps.append(step)
    return steps


def summarize_executed_steps(steps: list[ExecutedStep]) -> str | None:
    """Return a short spoken summary for executed steps."""
    if not steps:
        return None
    if len(steps) == 1:
        step = steps[0]
        joined_names = ", ".join(step.names) if step.names else "that"
        if step.kind == "lights":
            return f"Done. The {joined_names} lights are now {step.state}."
        if step.kind == "switches":
            return f"Done. The {joined_names} switches are now {step.state}."
        if step.kind == "media_player":
            return f"Done. {joined_names} is set to {step.state}."
        if step.kind == "climate":
            return f"Done. The temperature for {joined_names} is set to {step.state}."
        if step.kind == "wait":
            return f"Done. Waited {step.seconds} seconds."

    parts: list[str] = []
    for step in steps:
        joined_names = ", ".join(step.names) if step.names else "that"
        if step.kind == "lights":
            parts.append(f"turned the {joined_names} lights {step.state}")
        elif step.kind == "switches":
            parts.append(f"turned the {joined_names} switches {step.state}")
        elif step.kind == "media_player":
            parts.append(f"set {joined_names} to {step.state}")
        elif step.kind == "climate":
            parts.append(f"set {joined_names} to {step.state}")
        elif step.kind == "wait":
            parts.append(f"waited {step.seconds} seconds")
    if not parts:
        return None
    if len(parts) == 2:
        return f"Done. {parts[0].capitalize()}, then {parts[1]}."
    return f"Done. {parts[0].capitalize()}, " + ", ".join(parts[1:-1]) + f", then {parts[-1]}."
