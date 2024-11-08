"""OpenWebUI conversation agent."""

from __future__ import annotations

from typing import Literal

from hassil import recognize
from hassil.intents import Intents, WildcardSlotList

from homeassistant.components import assist_pipeline, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    TemplateError,
)
from homeassistant.helpers import intent
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid

from .api import OpenWebUIApiClient
from .const import (
    DOMAIN,
    LOGGER,
    DO_SEARCH_INTENT,
    CONF_SERVICE_NAME,
    CONF_BASE_URL,
    CONF_API_KEY,
    CONF_TIMEOUT,
    CONF_MODEL,
    CONF_LANGUAGE_CODE,
    CONF_SEARCH_ENABLED,
    CONF_SEARCH_SENTENCES,
    CONF_SEARCH_RESULT_PREFIX,
    DEFAULT_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_LANGUAGE_CODE,
    DEFAULT_SEARCH_ENABLED,
    DEFAULT_SEARCH_SENTENCES,
    DEFAULT_SEARCH_RESULT_PREFIX,
)
from .exceptions import ApiClientError, ApiCommError, ApiJsonError, ApiTimeoutError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> bool:
    """Set up OpenWebUI Conversation Agent from a config entry."""
    agent = OpenWebUIAgent(hass, entry)
    async_add_entities([agent])
    return True


class OpenWebUIAgent(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """OpenWebUI conversation agent."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry
        self.client = OpenWebUIApiClient(
            base_url=entry.data[CONF_BASE_URL],
            api_key=entry.data[CONF_API_KEY],
            timeout=entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
            session=async_get_clientsession(hass),
        )
        self.history: dict[str, dict] = {}
        self.search_enabled = entry.options.get(
            CONF_SEARCH_ENABLED, DEFAULT_SEARCH_ENABLED
        )
        self.search_sentences = [
            x
            for x in entry.options.get(
                CONF_SEARCH_SENTENCES, DEFAULT_SEARCH_SENTENCES
            ).splitlines()
            if x.strip()
        ]
        self.search_result_prefix = entry.options.get(
            CONF_SEARCH_RESULT_PREFIX, DEFAULT_SEARCH_RESULT_PREFIX
        )
        self.lang = entry.options.get(CONF_LANGUAGE_CODE, DEFAULT_LANGUAGE_CODE).strip()
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        assist_pipeline.async_migrate_engine(
            self.hass, "conversation", self.entry.entry_id, self.entity_id
        )
        conversation.async_set_agent(self.hass, self.entry, self)
        self.entry.async_on_unload(
            self.entry.add_update_listener(self._async_entry_update_listener)
        )

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a sentence."""

        if user_input.conversation_id in self.history:
            conversation_id = user_input.conversation_id
            messages = self.history[conversation_id]
        else:
            conversation_id = ulid.ulid()
            messages = {}

        messages["prompt"] = user_input.text

        should_search = False

        if self.search_enabled and len(self.search_sentences):
            i = Intents.from_dict(
                {
                    "language": self.lang,
                    "settings": {"ignore_whitespace": True},
                    "intents": {
                        DO_SEARCH_INTENT: {
                            "data": [{"sentences": self.search_sentences}]
                        }
                    },
                    "lists": {"query": {"wildcard": True}},
                }
            )
            r = recognize(messages["prompt"], i)
            if r is not None:
                if (
                    r.intent.name == DO_SEARCH_INTENT
                    and r.entities.get("query", None) is not None
                ):
                    messages["prompt"] = r.entities["query"].value
                    should_search = True

        try:
            response = None
            if should_search:
                response = await self.search(messages)
            else:
                response = await self.query(messages)
        except (ApiCommError, ApiJsonError, ApiTimeoutError) as err:
            LOGGER.error("Error generating prompt: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Something went wrong, {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )
        except HomeAssistantError as err:
            LOGGER.error("Something went wrong: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "Something went wrong, please check the logs for more information.",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=conversation_id
            )

        self.history[conversation_id] = messages

        intent_response = intent.IntentResponse(language=user_input.language)

        response_data = response["message"]["content"]
        if should_search:
            response_data = f"{self.search_result_prefix} {response_data}"
        intent_response.async_set_speech(response_data)
        return conversation.ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )

    async def query(self, messages) -> any:
        """Process a sentence."""
        model = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)

        LOGGER.debug("Prompt for %s: %s", model, messages["prompt"])

        result = await self.client.async_generate(
            {
                "model": model,
                "messages": [{"role": "user", "content": messages["prompt"]}],
                "stream": False,
            }
        )

        response: str = result["message"]["content"]
        LOGGER.debug("Response %s", response)
        return result

    async def search(self, messages) -> any:
        model_id = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)

        search_query = messages["prompt"]

        LOGGER.debug("Search for %s: %s", model_id, search_query)

        generated_query = await self.client.async_generate_search_query(
            {
                "model": model_id,
                "messages": [
                    {
                        "id": None,
                        "parentId": None,
                        "childrenIds": [],
                        "role": "user",
                        "content": search_query,
                        "models": [model_id],
                    }
                ],
                "prompt": search_query,
            }
        )
        generated_query_string = generated_query["choices"][0]["message"]["content"]

        search_results = await self.client.async_perform_search(
            {
                "query": generated_query_string,
                "collection_name": "",
            }
        )
        search_results_collection = search_results["collection_name"]
        search_results_filenames = search_results["filenames"]

        generated_output = await self.client.async_generate(
            {
                "stream": False,
                "model": "jarvis",
                "messages": [{"role": "user", "content": search_query}],
                "options": {},
                "keep_alive": "-1m",
                "files": [
                    {
                        "collection_name": search_results_collection,
                        "name": generated_query_string,
                        "type": "web_search_results",
                        "urls": search_results_filenames,
                    }
                ],
            }
        )
        return generated_output

    async def _async_entry_update_listener(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Handle options update."""
        # Reload as we update device info + entity name + supported features
        await hass.config_entries.async_reload(entry.entry_id)
