"""Local Home Assistant tool execution for OpenWebUI responses."""

from __future__ import annotations

import asyncio
from difflib import get_close_matches
import json
import re
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry

from .const import LOGGER
from .helpers import get_exposed_entities


@dataclass
class ExecutedStep:
    """A locally executed action."""

    kind: str
    names: list[str]
    entity_ids: list[str]
    state: str | None = None
    seconds: int | None = None


@dataclass
class ToolExecutionResult:
    """A locally executed tool call with structured result data."""

    tool_call_id: str
    tool_name: str
    parameters: dict[str, Any]
    step: ExecutedStep | None
    tool_result: dict[str, Any]


@dataclass
class ResolutionFailure:
    """Structured target-resolution failure details."""

    requested_names: list[str]
    matched_names: list[str]
    suggestions: list[str]
    message: str


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


def _lookup_key(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"^[Tt]he\s+", "", text)
    text = re.sub(r"[^\w\s\.]", " ", text)
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _lookup_variants(value: str) -> set[str]:
    base = _lookup_key(value)
    if not base:
        return set()
    variants = {base}
    suffixes = (" light", " lights", " switch", " switches")
    for suffix in suffixes:
        if base.endswith(suffix):
            trimmed = base[: -len(suffix)].strip()
            if trimmed:
                variants.add(trimmed)
        else:
            variants.add(f"{base}{suffix}")
    return variants


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
            normalized_calls.append(
                {
                    "id": str(
                        (tool_call or {}).get("id")
                        or f"tool_call_{len(normalized_calls) + 1}"
                    ),
                    "name": name,
                    "parameters": args,
                }
            )
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
        normalized_calls.append(
            {
                "id": str(
                    tool_call.get("id") or f"tool_call_{len(normalized_calls) + 1}"
                ),
                "name": name,
                "parameters": params,
            }
        )
    return normalized_calls


def _entity_index(hass: HomeAssistant) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for entity in get_exposed_entities(hass):
        entity_id = entity["entity_id"]
        entity_name = entity["name"]
        entity_slug = entity_id.split(".", 1)[-1]
        keys = {
            entity_id.lower(),
            entity_slug.lower(),
            entity_name.lower(),
            _lookup_key(entity_id),
            _lookup_key(entity_slug),
            _lookup_key(entity_name),
        }
        for value in (entity_id, entity_slug, entity_name):
            keys.update(_lookup_variants(value))
        for alias in entity.get("aliases", []):
            if alias:
                alias_text = str(alias)
                keys.add(alias_text.lower())
                keys.add(_lookup_key(alias_text))
                keys.update(_lookup_variants(alias_text))
        for key in keys:
            index.setdefault(key, []).append(entity)
    return index


def _resolve_via_alias_map(
    hass: HomeAssistant,
    names: list[str],
    alias_map: dict[str, str] | None,
    expected_domain: str | None = None,
) -> tuple[list[str], list[str]]:
    if not alias_map:
        return [], []

    state_ids = {state.entity_id for state in hass.states.async_all()}
    resolved_ids: list[str] = []
    resolved_names: list[str] = []

    for raw_name in names:
        raw_text = raw_name.strip()
        if not raw_text:
            continue
        entity_id = (
            alias_map.get(raw_text)
            or alias_map.get(raw_text.casefold())
            or alias_map.get(_lookup_key(raw_text))
        )
        if not entity_id:
            continue
        if expected_domain and not entity_id.startswith(f"{expected_domain}."):
            continue
        if entity_id not in state_ids:
            LOGGER.debug("Alias map resolved %r to missing entity %r", raw_name, entity_id)
            continue
        if entity_id not in resolved_ids:
            resolved_ids.append(entity_id)
            resolved_names.append(raw_text)

    return resolved_ids, resolved_names


def _resolve_entities(
    hass: HomeAssistant,
    names: list[str],
    expected_domain: str | None = None,
    alias_map: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    alias_ids, alias_names = _resolve_via_alias_map(
        hass, names, alias_map, expected_domain
    )
    if alias_ids:
        return alias_ids, alias_names

    index = _entity_index(hass)
    resolved_ids: list[str] = []
    resolved_names: list[str] = []
    for raw_name in names:
        raw_text = raw_name.strip()
        candidates: list[dict[str, Any]] = []
        seen_entity_ids: set[str] = set()
        for lookup_value in _lookup_variants(raw_text) | {raw_text.lower()}:
            for entity in index.get(lookup_value, []):
                if entity["entity_id"] in seen_entity_ids:
                    continue
                seen_entity_ids.add(entity["entity_id"])
                candidates.append(entity)
        if expected_domain:
            candidates = [
                entity
                for entity in candidates
                if entity["entity_id"].split(".", 1)[0] == expected_domain
            ]
        if not candidates:
            LOGGER.debug(
                "Unable to resolve entity for %r in domain %r", raw_name, expected_domain
            )
            continue
        entity = candidates[0]
        entity_id = entity["entity_id"]
        if entity_id not in resolved_ids:
            resolved_ids.append(entity_id)
            resolved_names.append(entity["name"])
    return resolved_ids, resolved_names


def _requested_target_names(parameters: dict[str, Any]) -> list[str]:
    entity_candidates = _entity_ids_from_parameters(parameters)
    if entity_candidates:
        return entity_candidates
    names = _normalize_name_list(parameters.get("names"))
    if not names:
        names = _normalize_name_list(parameters.get("name"))
    if not names:
        names = _normalize_name_list(parameters.get("names_csv"))
    return names


def _target_domain_for_tool(tool_name: str) -> str | None:
    name = _normalize_tool_name(tool_name)
    if name == "control_lights":
        return "light"
    if name == "control_switches":
        return "switch"
    if name == "media_player_command":
        return "media_player"
    if name == "climate_set_temperature":
        return "climate"
    return None


def _suggest_targets(
    hass: HomeAssistant,
    requested_names: list[str],
    expected_domain: str | None = None,
) -> list[str]:
    entities = get_exposed_entities(hass)
    candidates: list[str] = []
    normalized_to_display: dict[str, str] = {}
    for entity in entities:
        if expected_domain and not entity["entity_id"].startswith(f"{expected_domain}."):
            continue
        display_values = [entity["name"], entity["entity_id"], *entity.get("aliases", [])]
        for display_value in display_values:
            if not display_value:
                continue
            display_text = str(display_value).strip()
            normalized = _lookup_key(display_text)
            if not normalized:
                continue
            normalized_to_display.setdefault(normalized, display_text)
            candidates.append(normalized)

    suggestions: list[str] = []
    for requested_name in requested_names:
        requested_key = _lookup_key(requested_name)
        if not requested_key:
            continue
        for match in get_close_matches(requested_key, candidates, n=3, cutoff=0.6):
            display_value = normalized_to_display.get(match)
            if display_value and display_value not in suggestions:
                suggestions.append(display_value)
    return suggestions[:3]


def _build_resolution_failure(
    hass: HomeAssistant,
    tool_name: str,
    parameters: dict[str, Any],
) -> ResolutionFailure:
    requested_names = _requested_target_names(parameters)
    suggestions = _suggest_targets(
        hass, requested_names, _target_domain_for_tool(tool_name)
    )
    if requested_names:
        joined_requested = ", ".join(requested_names)
        message = (
            f"'{joined_requested}' is not available in your exposed Home Assistant "
            "names or aliases."
        )
    else:
        message = "The requested target is not available in your exposed Home Assistant names or aliases."
    return ResolutionFailure(
        requested_names=requested_names,
        matched_names=[],
        suggestions=suggestions,
        message=message,
    )


def _entity_ids_from_parameters(parameters: dict[str, Any]) -> list[str]:
    for key in ("entity_id", "entity_ids", "entityID", "entityIDs"):
        values = _normalize_name_list(parameters.get(key))
        if values:
            return values
    return []


def _names_or_ids_from_parameters(parameters: dict[str, Any]) -> list[str]:
    for key in (
        "names_or_ids",
    ):
        values = _normalize_name_list(parameters.get(key))
        if values:
            return values
    entities_csv = parameters.get("entities_csv")
    if isinstance(entities_csv, str) and entities_csv.strip():
        return [part.strip() for part in entities_csv.split(",") if part.strip()]
    return []


def _resolve_direct_entity_ids(
    hass: HomeAssistant,
    entity_ids: list[str],
    expected_domain: str | None = None,
) -> tuple[list[str], list[str]]:
    registry = entity_registry.async_get(hass)
    resolved_ids: list[str] = []
    resolved_names: list[str] = []
    for entity_id in entity_ids:
        candidate = entity_id.strip()
        if not candidate or "." not in candidate:
            continue
        if expected_domain and not candidate.startswith(f"{expected_domain}."):
            continue
        state = hass.states.get(candidate)
        if state is None:
            continue
        if candidate not in resolved_ids:
            resolved_ids.append(candidate)
            entry = registry.async_get(candidate)
            resolved_names.append(
                entry.original_name
                or entry.name
                or str(getattr(state, "name", "") or candidate)
            )
    return resolved_ids, resolved_names


def _resolve_entity_targets(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    expected_domain: str | None = None,
    alias_map: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    entity_candidates = _entity_ids_from_parameters(parameters)
    if entity_candidates:
        return _resolve_direct_entity_ids(hass, entity_candidates, expected_domain)
    names = _normalize_name_list(parameters.get("names"))
    if not names:
        names = _normalize_name_list(parameters.get("name"))
    if not names:
        names = _normalize_name_list(parameters.get("names_csv"))
    if not names:
        names = _names_or_ids_from_parameters(parameters)
    return _resolve_entities(hass, names, expected_domain, alias_map)


async def _call_service(
    hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]
) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


async def _execute_control_lights(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    alias_map: dict[str, str] | None = None,
) -> ExecutedStep | None:
    entity_ids, resolved_names = _resolve_entity_targets(
        hass, parameters, "light", alias_map
    )
    if not entity_ids:
        LOGGER.debug("control_lights could not resolve targets: %s", parameters)
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
    return ExecutedStep(
        "lights",
        resolved_names or entity_ids,
        entity_ids,
        "off" if service == "turn_off" else "on",
    )


async def _execute_control_switches(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    alias_map: dict[str, str] | None = None,
) -> ExecutedStep | None:
    entity_ids, resolved_names = _resolve_entity_targets(
        hass, parameters, "switch", alias_map
    )
    if not entity_ids:
        LOGGER.debug("control_switches could not resolve targets: %s", parameters)
        return None
    state = str(parameters.get("state", "")).strip().lower()
    service = "turn_off" if state == "off" else "turn_on"
    await _call_service(hass, "switch", service, {"entity_id": entity_ids})
    return ExecutedStep(
        "switches",
        resolved_names or entity_ids,
        entity_ids,
        "off" if service == "turn_off" else "on",
    )


async def _execute_media_player_command(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    alias_map: dict[str, str] | None = None,
) -> ExecutedStep | None:
    entity_ids, resolved_names = _resolve_entity_targets(
        hass, parameters, "media_player", alias_map
    )
    if not entity_ids:
        LOGGER.debug("media_player_command could not resolve targets: %s", parameters)
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
    return ExecutedStep(
        "media_player", resolved_names or entity_ids, entity_ids, action
    )


async def _execute_climate_set_temperature(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    alias_map: dict[str, str] | None = None,
) -> ExecutedStep | None:
    entity_ids, resolved_names = _resolve_entity_targets(
        hass, parameters, "climate", alias_map
    )
    if not entity_ids:
        LOGGER.debug(
            "climate_set_temperature could not resolve targets: %s", parameters
        )
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
    return ExecutedStep(
        "climate",
        resolved_names or entity_ids,
        entity_ids,
        f"{float(temperature_c):g}C",
    )


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
    return ExecutedStep("wait", [], [], seconds=seconds)


async def _execute_control_device(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    alias_map: dict[str, str] | None = None,
) -> ExecutedStep | None:
    entity_ids, resolved_names = _resolve_entity_targets(
        hass, parameters, alias_map=alias_map
    )
    if not entity_ids:
        LOGGER.debug("controlDevice could not resolve targets: %s", parameters)
        return None
    domain = str(parameters.get("domain", "")).strip()
    service = str(parameters.get("service", "")).strip()
    if not domain or not service:
        LOGGER.debug("controlDevice missing domain/service: %s", parameters)
        return None
    await _call_service(hass, domain, service, {"entity_id": entity_ids})
    return ExecutedStep(
        "service",
        resolved_names or entity_ids,
        entity_ids,
        f"{domain}.{service}",
    )


async def _execute_call_service_raw(
    hass: HomeAssistant,
    parameters: dict[str, Any],
    alias_map: dict[str, str] | None = None,
) -> ExecutedStep | None:
    domain = str(parameters.get("domain", "")).strip()
    service = str(parameters.get("service", "")).strip()
    if not domain or not service:
        LOGGER.debug("call_service_raw missing domain/service: %s", parameters)
        return None
    entity_ids, resolved_names = _resolve_entity_targets(
        hass, parameters, alias_map=alias_map
    )
    if not entity_ids:
        LOGGER.debug("call_service_raw could not resolve targets: %s", parameters)
        return None
    data: dict[str, Any] = {"entity_id": entity_ids}
    data_json = parameters.get("data_json")
    if isinstance(data_json, str) and data_json.strip():
        try:
            parsed_data = json.loads(data_json)
        except Exception:
            parsed_data = None
        if isinstance(parsed_data, dict):
            data.update(parsed_data)
    await _call_service(hass, domain, service, data)
    return ExecutedStep(
        "service",
        resolved_names or entity_ids,
        entity_ids,
        f"{domain}.{service}",
    )


def _tool_result_from_step(step: ExecutedStep) -> dict[str, Any]:
    """Convert an executed step into a tool result payload."""
    result: dict[str, Any] = {
        "success": True,
        "kind": step.kind,
        "names": step.names,
        "entity_ids": step.entity_ids,
    }
    if step.state is not None:
        result["state"] = step.state
    if step.seconds is not None:
        result["seconds"] = step.seconds
    return result


def _display_targets(values: list[str]) -> str:
    if not values:
        return "that"
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return ", ".join(values[:-1]) + f", and {values[-1]}"


def describe_tool_call(tool_name: str, parameters: dict[str, Any]) -> str | None:
    """Return a short narrated progress line for a planned tool call."""
    name = _normalize_tool_name(tool_name)
    targets = _normalize_name_list(parameters.get("names"))
    if not targets:
        targets = _normalize_name_list(parameters.get("name"))
    if name == "control_lights":
        state = str(parameters.get("state", "on")).strip().lower() or "on"
        return f"Turning {state} {_display_targets(targets)}."
    if name == "control_switches":
        state = str(parameters.get("state", "on")).strip().lower() or "on"
        return f"Switching {state} {_display_targets(targets)}."
    if name == "media_player_command":
        action = str(parameters.get("action", "control")).strip().lower() or "control"
        return f"{action.capitalize()} {_display_targets(targets)}."
    if name == "climate_set_temperature":
        temperature_c = parameters.get("temperature_c")
        if temperature_c is not None:
            return f"Setting {_display_targets(targets)} to {temperature_c}C."
        return f"Adjusting {_display_targets(targets)}."
    if name == "wait":
        seconds = parameters.get("seconds", parameters.get("duration"))
        try:
            seconds_value = max(0, min(int(float(seconds)), 60))
        except Exception:
            seconds_value = None
        if seconds_value is not None:
            return f"Waiting {seconds_value} seconds."
        return "Waiting."
    if name == "controlDevice":
        return f"Calling {_display_targets(targets)}."
    if name == "call_service_raw":
        domain = str(parameters.get("domain", "")).strip()
        service = str(parameters.get("service", "")).strip()
        if domain and service:
            return f"Calling {domain}.{service} for {_display_targets(targets)}."
        return f"Calling a Home Assistant service for {_display_targets(targets)}."
    return None


def describe_tool_execution_result(result: ToolExecutionResult) -> str | None:
    """Return a short narrated progress line for an executed tool step."""
    if result.step is None:
        requested_names = result.tool_result.get("requested_names") or []
        if requested_names:
            joined_names = _display_targets([str(name) for name in requested_names])
            return (
                f"I couldn't find {joined_names} in your exposed Home Assistant names "
                "or aliases."
            )
        if result.tool_name == "wait":
            return "I couldn't wait as requested."
        return "I couldn't complete that step."

    step = result.step
    joined_names = _display_targets(step.names or step.entity_ids)
    if step.kind == "lights":
        return f"{joined_names} is now {step.state}."
    if step.kind == "switches":
        return f"{joined_names} is now {step.state}."
    if step.kind == "media_player":
        return f"{joined_names} is now {step.state}."
    if step.kind == "climate":
        return f"{joined_names} is set to {step.state}."
    if step.kind == "wait":
        return f"Finished waiting {step.seconds} seconds."
    if step.kind == "service":
        return f"Called {step.state} for {joined_names}."
    return None


async def execute_tool_calls_detailed(
    hass: HomeAssistant,
    tool_calls: list[dict[str, Any]],
    alias_map: dict[str, str] | None = None,
) -> list[ToolExecutionResult]:
    """Execute supported tool calls in order with structured results."""
    results: list[ToolExecutionResult] = []
    for index, tool_call in enumerate(tool_calls, start=1):
        tool_call_id = str(tool_call.get("id") or f"tool_call_{index}")
        name = _normalize_tool_name(tool_call.get("name"))
        parameters = tool_call.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        step: ExecutedStep | None = None
        if name == "control_lights":
            step = await _execute_control_lights(hass, parameters, alias_map)
        elif name == "control_switches":
            step = await _execute_control_switches(hass, parameters, alias_map)
        elif name == "media_player_command":
            step = await _execute_media_player_command(hass, parameters, alias_map)
        elif name == "climate_set_temperature":
            step = await _execute_climate_set_temperature(hass, parameters, alias_map)
        elif name == "wait":
            step = await _execute_wait(parameters)
        elif name == "controlDevice":
            step = await _execute_control_device(hass, parameters, alias_map)
        elif name == "call_service_raw":
            step = await _execute_call_service_raw(hass, parameters, alias_map)
        else:
            LOGGER.debug("Ignoring unsupported local tool call: %s", name)
        if step is None:
            LOGGER.debug("Tool call produced no executed step: %s", tool_call)
            if name in {
                "control_lights",
                "control_switches",
                "media_player_command",
                "climate_set_temperature",
                "controlDevice",
                "call_service_raw",
            }:
                failure = _build_resolution_failure(hass, name, parameters)
                tool_result = {
                    "success": False,
                    "error": "unresolved_target",
                    "tool_name": name or "unknown",
                    "requested_names": failure.requested_names,
                    "matched_names": failure.matched_names,
                    "suggestions": failure.suggestions,
                    "message": failure.message,
                }
            else:
                tool_result = {
                    "success": False,
                    "error": "unsupported_or_unresolved_tool_call",
                    "tool_name": name or "unknown",
                }
        else:
            tool_result = _tool_result_from_step(step)
        results.append(
            ToolExecutionResult(
                tool_call_id=tool_call_id,
                tool_name=name or "unknown",
                parameters=parameters,
                step=step,
                tool_result=tool_result,
            )
        )
    return results


async def execute_tool_calls(
    hass: HomeAssistant,
    tool_calls: list[dict[str, Any]],
    alias_map: dict[str, str] | None = None,
) -> list[ExecutedStep]:
    """Execute supported tool calls in order."""
    results = await execute_tool_calls_detailed(hass, tool_calls, alias_map)
    return [result.step for result in results if result.step is not None]


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
        if step.kind == "service":
            return f"Done. Called {step.state} for {joined_names}."

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
        elif step.kind == "service":
            parts.append(f"called {step.state} for {joined_names}")
    if not parts:
        return None
    if len(parts) == 2:
        return f"Done. {parts[0].capitalize()}, then {parts[1]}."
    return f"Done. {parts[0].capitalize()}, " + ", ".join(parts[1:-1]) + f", then {parts[-1]}."


def summarize_execution_results(results: list[ToolExecutionResult]) -> str | None:
    """Return a short spoken summary for mixed success/failure runs."""
    if not results:
        return None

    failures = [result for result in results if result.step is None]
    steps = [result.step for result in results if result.step is not None]
    success_summary = summarize_executed_steps(steps)

    if not failures:
        return success_summary

    failure_messages: list[str] = []
    seen_failures: set[tuple[str, ...]] = set()
    for failure in failures:
        requested_names = tuple(
            str(name) for name in (failure.tool_result.get("requested_names") or [])
        )
        if requested_names in seen_failures:
            continue
        seen_failures.add(requested_names)
        if requested_names:
            joined_names = _display_targets(list(requested_names))
            message = (
                f"I couldn't find {joined_names} in your exposed Home Assistant names "
                "or aliases."
            )
            suggestions = failure.tool_result.get("suggestions") or []
            if suggestions:
                message += f" Try {_display_targets([str(s) for s in suggestions])}."
            failure_messages.append(message)
        else:
            failure_messages.append("I couldn't complete every requested step.")

    if not success_summary:
        return " ".join(failure_messages)

    trimmed_success = success_summary.removeprefix("Done. ").strip()
    return " ".join(failure_messages + [trimmed_success])
