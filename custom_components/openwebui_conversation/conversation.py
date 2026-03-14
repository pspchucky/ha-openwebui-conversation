"""OpenWebUI conversation agent."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
from typing import Any, Literal

from hassil import recognize
from hassil.intents import Intents

from homeassistant.components import assist_pipeline, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent, llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from markdown_it import MarkdownIt
from mdit_plain.renderer import RendererPlain

from .api import OpenWebUIApiClient
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_ENABLE_STREAMING,
    CONF_LANGUAGE_CODE,
    CONF_MODEL,
    CONF_SEARCH_ENABLED,
    CONF_SEARCH_RESULT_PREFIX,
    CONF_SEARCH_SENTENCES,
    CONF_SHOW_DEBUG_BUBBLES,
    CONF_STRIP_MARKDOWN,
    CONF_TIMEOUT,
    CONF_VERIFY_SSL,
    DEFAULT_ENABLE_STREAMING,
    DEFAULT_LANGUAGE_CODE,
    DEFAULT_MODEL,
    DEFAULT_SEARCH_ENABLED,
    DEFAULT_SEARCH_RESULT_PREFIX,
    DEFAULT_SEARCH_SENTENCES,
    DEFAULT_SHOW_DEBUG_BUBBLES,
    DEFAULT_STRIP_MARKDOWN,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_SSL,
    DO_SEARCH_INTENT,
    LOGGER,
)
from .exceptions import ApiCommError, ApiJsonError, ApiTimeoutError
from .local_executor import (
    ToolExecutionResult,
    execute_tool_calls_detailed,
    extract_tool_calls,
    summarize_executed_steps,
)

TOOL_ID_CACHE: dict[str, list[str]] = {}
LOCAL_TOOL_SYSTEM_PROMPT = """You can control Home Assistant locally by returning tool calls.

Supported tool names:
- home_assistant_tool/control_lights
- home_assistant_tool/control_switches
- home_assistant_tool/media_player_command
- home_assistant_tool/climate_set_temperature
- home_assistant_tool/wait

The wait tool takes: {"seconds": <integer>}

For device actions, respond with either native tool_calls or a JSON object in message content using this exact shape:
{"tool_calls":[{"name":"home_assistant_tool/control_lights","parameters":{"names":["Example"],"state":"on"}}]}

For multi-step requests, return multiple tool calls in the correct order. If the user asks to wait before another action, include a wait tool call between those actions.
"""


def _flatten_text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts)
    return ""


def _assistant_text_from_response(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    return _flatten_text_content(message.get("content"))


def _assistant_reasoning_from_response(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    for key in ("reasoning", "reasoning_content", "thinking_content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_inputs_from_tool_calls(tool_calls: list[dict[str, Any]]) -> list[llm.ToolInput]:
    tool_inputs: list[llm.ToolInput] = []
    for index, tool_call in enumerate(tool_calls, start=1):
        name = str(tool_call.get("name", "")).strip()
        parameters = tool_call.get("parameters")
        if not name or not isinstance(parameters, dict):
            continue
        tool_inputs.append(
            llm.ToolInput(
                tool_name=name,
                tool_args=parameters,
                id=str(tool_call.get("id") or f"tool_call_{index}"),
                external=True,
            )
        )
    return tool_inputs


def _normalize_stream_tool_calls(
    partial_tool_calls: dict[int, dict[str, str]],
) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for index in sorted(partial_tool_calls):
        partial = partial_tool_calls[index]
        name = partial.get("name", "").strip()
        if not name:
            continue
        arguments_text = partial.get("arguments", "").strip()
        parameters: dict[str, Any] = {}
        if arguments_text:
            try:
                parsed = json.loads(arguments_text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                parameters = parsed
        tool_calls.append(
            {
                "id": partial.get("id") or f"tool_call_{index + 1}",
                "name": name,
                "parameters": parameters,
            }
        )
    return tool_calls


def _accumulate_stream_tool_calls(
    partial_tool_calls: dict[int, dict[str, str]],
    delta_tool_calls: list[dict[str, Any]],
) -> None:
    for delta_tool_call in delta_tool_calls:
        if not isinstance(delta_tool_call, dict):
            continue
        index = delta_tool_call.get("index")
        if not isinstance(index, int):
            index = len(partial_tool_calls)
        partial = partial_tool_calls.setdefault(
            index, {"id": "", "name": "", "arguments": ""}
        )
        tool_call_id = delta_tool_call.get("id")
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            partial["id"] = tool_call_id.strip()
        function = delta_tool_call.get("function") or {}
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                partial["name"] += name
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                partial["arguments"] += arguments


def _delta_text(delta: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _format_final_text(
    text: str,
    *,
    strip_markdown: bool,
    markdown_parser: MarkdownIt,
    search_prefix: str | None = None,
) -> str:
    final_text = text.strip()
    if strip_markdown and final_text:
        final_text = markdown_parser.render(final_text)
    if search_prefix and final_text:
        final_text = f"{search_prefix} {final_text}"
    return final_text


def _messages_from_chat_log(
    chat_log: conversation.ChatLog,
    prompt: str,
    *,
    include_local_tool_prompt: bool,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if include_local_tool_prompt:
        messages.append({"role": "system", "content": LOCAL_TOOL_SYSTEM_PROMPT})

    for content in chat_log.content[:-1]:
        role = getattr(content, "role", "")
        if role == "system" and getattr(content, "content", None):
            messages.append({"role": "system", "content": content.content})
        elif role == "user" and getattr(content, "content", None):
            messages.append({"role": "user", "content": content.content})
        elif role == "assistant" and getattr(content, "content", None):
            messages.append({"role": "assistant", "content": content.content})

    messages.append({"role": "user", "content": prompt})
    return messages


class OpenWebUIAgent(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """OpenWebUI conversation agent."""

    _attr_has_entity_name = True
    _attr_supports_streaming = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.hass = hass
        self.entry = entry
        self.timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        self.client = OpenWebUIApiClient(
            base_url=entry.data[CONF_BASE_URL],
            api_key=entry.data[CONF_API_KEY],
            timeout=self.timeout,
            session=async_get_clientsession(hass),
            verify_ssl=entry.options.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        )
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
        self.strip_markdown = entry.options.get(
            CONF_STRIP_MARKDOWN, DEFAULT_STRIP_MARKDOWN
        )
        self.enable_streaming = entry.options.get(
            CONF_ENABLE_STREAMING, DEFAULT_ENABLE_STREAMING
        )
        self.show_debug_bubbles = entry.options.get(
            CONF_SHOW_DEBUG_BUBBLES, DEFAULT_SHOW_DEBUG_BUBBLES
        )
        self.markdown_parser = MarkdownIt(renderer_cls=RendererPlain)

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

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Process a sentence."""
        prompt, should_search = self._prepare_prompt(user_input.text)

        try:
            tool_ids = await self._async_get_tool_ids()
            payload = {
                "features": {"web_search": should_search},
                "tool_ids": tool_ids,
                "model": self.entry.options.get(CONF_MODEL, DEFAULT_MODEL),
                "messages": _messages_from_chat_log(
                    chat_log,
                    prompt,
                    include_local_tool_prompt=not tool_ids,
                ),
                "params": {"keep_alive": "-1m"},
                "options": {"keep_alive": -1},
            }

            if self.enable_streaming:
                async for _content in chat_log.async_add_delta_content_stream(
                    self.entity_id,
                    self._async_stream_chat(payload, should_search=should_search),
                ):
                    pass
            else:
                response = await self.client.async_generate({**payload, "stream": False})
                await self._async_add_nonstream_response(
                    chat_log,
                    response,
                    should_search=should_search,
                )
        except (ApiCommError, ApiJsonError, ApiTimeoutError) as err:
            LOGGER.error("Error generating prompt: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Something went wrong, {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=chat_log.conversation_id
            )
        except HomeAssistantError as err:
            LOGGER.error("Something went wrong: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "Something went wrong, please check the logs for more information.",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=chat_log.conversation_id
            )

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    def _prepare_prompt(self, prompt: str) -> tuple[str, bool]:
        """Apply search trigger detection."""
        if not (self.search_enabled and self.search_sentences):
            return prompt, False

        intents = Intents.from_dict(
            {
                "language": self.lang,
                "settings": {"ignore_whitespace": True},
                "intents": {DO_SEARCH_INTENT: {"data": [{"sentences": self.search_sentences}]}},
                "lists": {"query": {"wildcard": True}},
            }
        )
        recognized = recognize(prompt, intents)
        if recognized is None:
            return prompt, False
        if (
            recognized.intent.name == DO_SEARCH_INTENT
            and recognized.entities.get("query") is not None
        ):
            return recognized.entities["query"].value, True
        return prompt, False

    async def _async_get_tool_ids(self) -> list[str]:
        """Fetch model metadata and cache tool ids."""
        model = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        tool_ids = TOOL_ID_CACHE.get(model)
        if tool_ids is not None:
            return tool_ids

        models = await self.client.async_get_models()
        matching_model = next((m for m in models if m["id"] == model), {})
        tool_ids = matching_model.get("info", {}).get("meta", {}).get("toolIds", [])
        TOOL_ID_CACHE[model] = tool_ids
        LOGGER.debug("Using tool_ids for model %s: %s", model, tool_ids)
        return tool_ids

    async def _async_add_nonstream_response(
        self,
        chat_log: conversation.ChatLog,
        response: dict[str, Any],
        *,
        should_search: bool,
    ) -> None:
        """Add a non-streamed response to the chat log."""
        response_text = _assistant_text_from_response(response)
        reasoning_text = _assistant_reasoning_from_response(response)
        tool_calls = extract_tool_calls(response)
        execution_results: list[ToolExecutionResult] = []

        if tool_calls:
            execution_results = await execute_tool_calls_detailed(self.hass, tool_calls)
            steps = [result.step for result in execution_results if result.step is not None]
            response_text = (
                summarize_executed_steps(steps)
                or response_text
                or "I found a tool call, but couldn't execute it successfully."
            )
        elif not response_text:
            response_text = "I didn't get a usable response from the model."

        final_text = _format_final_text(
            response_text,
            strip_markdown=self.strip_markdown,
            markdown_parser=self.markdown_parser,
            search_prefix=self.search_result_prefix if should_search else None,
        )
        await self._async_add_structured_response(
            chat_log,
            reasoning_text=reasoning_text,
            tool_calls=tool_calls,
            execution_results=execution_results,
            final_text=final_text,
        )

    async def _async_add_structured_response(
        self,
        chat_log: conversation.ChatLog,
        *,
        reasoning_text: str,
        tool_calls: list[dict[str, Any]],
        execution_results: list[ToolExecutionResult],
        final_text: str,
    ) -> None:
        """Store the response in the chat log with separate assistant entries."""
        if self.show_debug_bubbles and reasoning_text:
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(
                    agent_id=self.entity_id,
                    thinking_content=reasoning_text,
                )
            )

        if self.show_debug_bubbles and tool_calls:
            tool_inputs = _tool_inputs_from_tool_calls(tool_calls)
            if tool_inputs:
                chat_log.async_add_assistant_content_without_tools(
                    conversation.AssistantContent(
                        agent_id=self.entity_id,
                        tool_calls=tool_inputs,
                    )
                )

        if self.show_debug_bubbles:
            for execution_result in execution_results:
                chat_log.async_add_assistant_content_without_tools(
                    conversation.ToolResultContent(
                        agent_id=self.entity_id,
                        tool_call_id=execution_result.tool_call_id,
                        tool_name=execution_result.tool_name,
                        tool_result=execution_result.tool_result,
                    )
                )

        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=self.entity_id,
                content=final_text,
            )
        )

    async def _async_stream_chat(
        self,
        payload: dict[str, Any],
        *,
        should_search: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream the response into Home Assistant chat log deltas."""
        partial_tool_calls: dict[int, dict[str, str]] = {}
        reasoning_seen = False
        content_parts: list[str] = []

        async for chunk in self.client.async_generate_stream({**payload, "stream": True}):
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] or {}
            delta = choice.get("delta") or choice.get("message") or {}

            reasoning_delta = _delta_text(
                delta, "reasoning", "reasoning_content", "thinking_content"
            )
            if reasoning_delta:
                reasoning_seen = True
                if self.show_debug_bubbles:
                    yield {"thinking_content": reasoning_delta}

            delta_tool_calls = delta.get("tool_calls")
            if isinstance(delta_tool_calls, list):
                _accumulate_stream_tool_calls(partial_tool_calls, delta_tool_calls)

            content_delta = _flatten_text_content(delta.get("content"))
            if content_delta:
                content_parts.append(content_delta)

        tool_calls = _normalize_stream_tool_calls(partial_tool_calls)
        final_content = "".join(content_parts).strip()

        if not tool_calls and final_content:
            prompt_plan = extract_tool_calls(
                {"choices": [{"message": {"content": final_content}}]}
            )
            if prompt_plan:
                tool_calls = prompt_plan
                final_content = ""

        if tool_calls:
            if self.show_debug_bubbles and reasoning_seen:
                yield {"role": "assistant"}
            if self.show_debug_bubbles:
                tool_inputs = _tool_inputs_from_tool_calls(tool_calls)
                if tool_inputs:
                    yield {"role": "assistant", "tool_calls": tool_inputs}

            execution_results = await execute_tool_calls_detailed(self.hass, tool_calls)
            if self.show_debug_bubbles:
                for execution_result in execution_results:
                    yield {
                        "role": "tool_result",
                        "tool_call_id": execution_result.tool_call_id,
                        "tool_name": execution_result.tool_name,
                        "tool_result": execution_result.tool_result,
                    }

            steps = [result.step for result in execution_results if result.step is not None]
            final_text = summarize_executed_steps(steps)
            if not final_text:
                final_text = (
                    final_content
                    or "I found a tool call, but couldn't execute it successfully."
                )
        else:
            final_text = final_content or "I didn't get a usable response from the model."

        final_text = _format_final_text(
            final_text,
            strip_markdown=self.strip_markdown,
            markdown_parser=self.markdown_parser,
            search_prefix=self.search_result_prefix if should_search else None,
        )
        yield {"role": "assistant", "content": final_text}

    async def _async_entry_update_listener(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Handle options update."""
        await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> bool:
    """Set up OpenWebUI Conversation Agent from a config entry."""
    agent = OpenWebUIAgent(hass, entry)
    async_add_entities([agent])
    return True
