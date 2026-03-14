"""Helper functions for OpenWebUI."""

from homeassistant.helpers import area_registry, device_registry
from homeassistant.components.conversation import DOMAIN as CONVERSATION_DOMAIN
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry


def get_exposed_entities(hass: HomeAssistant) -> list[dict]:
    """Return exposed entities."""
    hass_entity = entity_registry.async_get(hass)
    hass_device = device_registry.async_get(hass)
    hass_area = area_registry.async_get(hass)
    exposed_entities: list[dict] = []

    for state in hass.states.async_all():
        if async_should_expose(hass, CONVERSATION_DOMAIN, state.entity_id):
            entity = hass_entity.async_get(state.entity_id)
            aliases = list(entity.aliases) if entity and entity.aliases else []
            area_names: list[str] = []
            area_id = entity.area_id if entity else None
            if area_id:
                if area_entry := hass_area.async_get_area(area_id):
                    area_names.append(area_entry.name)
            elif entity and entity.device_id:
                if device := hass_device.async_get(entity.device_id):
                    if device.area_id and (area_entry := hass_area.async_get_area(device.area_id)):
                        area_names.append(area_entry.name)

            for area_name in area_names:
                if area_name and area_name not in aliases:
                    aliases.append(area_name)
            exposed_entities.append(
                {
                    "entity_id": state.entity_id,
                    "name": state.name,
                    "state": state.state,
                    "aliases": aliases,
                }
            )

    return exposed_entities
