[![GitHub Release](https://img.shields.io/github/release/TheRealPSV/ha-openwebui-conversation.svg?style=flat-square)](https://github.com/TheRealPSV/ha-openwebui-conversation/releases)
[![Downloads](https://img.shields.io/github/downloads/TheRealPSV/ha-openwebui-conversation/total?style=flat-square)](https://github.com/TheRealPSV/ha-openwebui-conversation/releases)
[![Build Status](https://img.shields.io/github/actions/workflow/status/TheRealPSV/ha-openwebui-conversation/validate.yml?style=flat-square)](https://github.com/TheRealPSV/ha-openwebui-conversation/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/TheRealPSV/ha-openwebui-conversation.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-default-blue.svg?style=flat-square)](https://hacs.xyz)

# OpenWebUI Conversation

This fork adds local Home Assistant action execution on top of the upstream OpenWebUI conversation flow.

The OpenWebUI integration adds a conversation agent powered by [OpenWebUI][openwebui] in Home Assistant.

Unlike the upstream project, this fork can execute supported Home Assistant actions locally when the model returns either native `tool_calls` or a prompt-style JSON tool plan in `message.content`.

It also aligns more closely with the current upstream Assist clients:

* web can consume native structured tool progress through `tool_calls` and `tool_result`
* iOS can consume readable streamed assistant `content` for normal non-tool replies
* raw model chain-of-thought is not forwarded to users
* tool runs suppress provisional model prose so the final Assist speech stays reliable

Supported local tools:

* `home_assistant_tool/control_lights`
* `home_assistant_tool/control_switches`
* `home_assistant_tool/media_player_command`
* `home_assistant_tool/climate_set_temperature`
* `home_assistant_tool/wait`
* `home_assistant_tool/light_on_then_off_after_delay` (example OpenWebUI tool script)

This makes multi-step local sequences possible, including patterns like "turn on the middle bedroom lights, wait 5 seconds, then turn them off", as long as the model returns the tool calls in order.

This conversation agent can search the internet for you, using sentence triggers you can configure, if Web Search is set up in OpenWebUI. For more details, see the relevant Options section below.

You can also take advantage of OpenWebUI's ability to "clone" models; once you create a clone model in OpenWebUI, it will automatically be available to select in the integration's options.

For best results with local tool execution, use an OpenWebUI model/workspace that either:

* has **Native Tool Calling** enabled, or
* returns a JSON object in `message.content` with a top-level `tool_calls` array.

## Example Files

This fork now includes a ready-to-copy example set for the NaBu + MLX + Home Assistant flow:

* [`examples/nabu_system_prompt.md`](examples/nabu_system_prompt.md) - example system prompt for the NaBu model/workspace in OpenWebUI.
* [`examples/home_assistant_pro_tools.py`](examples/home_assistant_pro_tools.py) - example OpenWebUI tool script for Home Assistant actions, including `wait` and `light_on_then_off_after_delay`.
* [`examples/native_multistep_request.json`](examples/native_multistep_request.json) - example OpenAI-compatible request body for a delayed multi-step light action.
* [`examples/native_multistep_response.json`](examples/native_multistep_response.json) - example native tool-calling response shape returned by the model.

These are meant to be practical examples you can adapt directly instead of rebuilding the setup from scratch.

When your NaBu/OpenWebUI system prompt includes a `Home Layout` section like:

* `Middle bedroom -> light.michaels_old_room`

the preferred path is now:

1. the MLX/OpenWebUI gateway reads that prompt
2. the gateway injects normalized `entity_ids` into tool arguments
3. the Home Assistant fork trusts those `entity_ids` first during local execution

The fork no longer depends on bundled NaBu room-name fallbacks for the primary path. Generic fallback resolution still exists through:

* `Local Alias Overrides`
* Home Assistant exposed names and aliases
* Home Assistant area names for exposed entities

If you want a guaranteed local mapping, you can also add manual alias overrides in the integration options using lines like:

* `Middle bedroom -> light.michaels_old_room`
* `Box fan -> switch.fan_outlet_2`

## Recommended Native Tool Setup

For the most reliable multi-step native tool execution in this fork:

* Enable **Native Tool Calling** on the OpenWebUI model.
* Use the NaBu prompt from [`examples/nabu_system_prompt.md`](examples/nabu_system_prompt.md).
* Load the OpenWebUI tool from [`examples/home_assistant_pro_tools.py`](examples/home_assistant_pro_tools.py).
* Point OpenWebUI at your MLX OpenAI-compatible endpoint.
* Prefer a stronger planning model for multi-step chains. In the tested MLX setup, `Qwen3.5-9B-MLX-4bit` was the most reliable option for `turn on -> wait -> turn off` style requests on Apple Silicon with constrained memory.

## Relevant Fork Files

If you are customizing or debugging this fork, these files are the important ones:

* [`custom_components/openwebui_conversation/conversation.py`](custom_components/openwebui_conversation/conversation.py)
  * Builds the message list sent to OpenWebUI.
  * Reads either one-shot or streamed model responses.
  * Keeps stable tool runs quiet by default, while exposing an experimental live hook mode for current Assist clients.
* [`custom_components/openwebui_conversation/local_executor.py`](custom_components/openwebui_conversation/local_executor.py)
  * Extracts native or prompt-style tool plans.
  * Executes supported Home Assistant actions locally in order.
  * Trusts explicit `entity_ids` first, then falls back to local overrides and exposed Home Assistant names.
* [`custom_components/openwebui_conversation/api.py`](custom_components/openwebui_conversation/api.py)
  * Handles the HTTP call to OpenWebUI.
  * Supports both one-shot JSON responses and streamed SSE responses.

## Example Flow

Example user request:

* `Turn on the middle bedroom light, wait 5 seconds, then turn off the middle bedroom light.`

Expected native tool plan:

1. `control_lights({"names":["light.michaels_old_room"],"state":"on"})`
2. `wait({"seconds":5})`
3. `control_lights({"names":["light.michaels_old_room"],"state":"off"})`

If you want to make that even easier for smaller models, the example tool script also exposes:

* `light_on_then_off_after_delay({"names":["Middle bedroom"],"seconds":5})`

That composite tool gives the model a simpler single decision when multi-step planning quality is not good enough.

## Installation

To install the **OpenWebUI Conversation** integration to your Home Assistant instance, use this My button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=therealpsv&repository=ha-openwebui-conversation&category=integration)

#### Manual Installation
If the above button doesn’t work, you can also perform the following steps manually:

* Browse to your Home Assistant instance.
* Go to HACS > Integrations > Custom Repositories.
* Add custom repository.
  * Repository is `TheRealPSV/ha-openwebui-conversation`.
  * Category is `Integration`.
* Click ***Explore & Download Repositories***.
* From the list, select OpenWebUI Conversation.
* In the bottom right corner, click the ***Download*** button.
* Follow the instructions on screen to complete the installation.

#### Note:
HACS does not "configure" the integration for you, You must add OpenWebUI Conversation after installing via HACS.

* Browse to your Home Assistant instance.
* Go to Settings > Devices & Services.
* In the bottom right corner, select the ***Add Integration*** button.
* From the list, select OpenWebUI Conversation.
* Follow the instructions on screen to complete the setup.
  * **Service Name** is required, but you can name it whatever you like.
  * **Base Url** is the URL for the OpenWebUI service.
  * **API Key** is the API key for your user, which you can find in your OpenWebUI Settings, under Account.
  * **API Timeout** is described below under General Settings.
  * **Verify SSL** is if requests should verify SSL certificates for HTTPS. Disable verification if you are using self signed certificates.
* Once you have added the integration, make sure you set your preferred model as described below.

## Options
Options for OpenWebUI Conversation can be set via the user interface, by taking the following steps:

* Browse to your Home Assistant instance.
* Go to Settings > Devices & Services.
* If multiple instances of OpenWebUI Conversation are configured, choose the instance you want to configure.
* Select the integration, then select ***Configure***.

#### General Settings
Settings relating to the integration itself.

| Option        | Description                                                                                                                      |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| API Timeout   | The maximum amount of time (in seconds) to wait for a response from the API                                                      |
| Language Code | The code for your preferred language. This is set to English (`en`) by default. A list of codes can be found [here][lang-codes]. |
| Verify SSL    | Verify SSL certificates for HTTPS. Disable verification if you are using self signed certificates.                               |
| Enable Streaming | Uses OpenWebUI's streaming API so Assist can show streamed replies and structured tool activity before the final spoken reply. |
| Narrate Streaming Progress | Experimental live tool-run hook. This intentionally reuses current Assist streaming behavior so some clients can speak or display live progress again. iOS may duplicate the final text in this mode. |
| Show Structured Tool Details | Stores native tool calls and tool results as separate Assist chat entries for clients that can render them. |
| Local Alias Overrides | Optional manual `Friendly name -> entity_id` mappings used by the local executor before other fallback resolution paths. |

#### Model Configuration
The language model you want to use.

| Option         | Description                                                                                                                                                                                                                                                                                |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Model          | The model used to generate responses. This list should automatically populate based on the models you have created in OpenWebUI.                                                                                                                                                           |
| Strip Markdown | Whether or not to strip Markdown formatting from the model's output. This can be useful for models that tend to generate responses with Markdown formatting, as HomeAssistant doesn't render Markdown text, and TTS engines will often read out individual Markdown formatting characters. |

NOTE: Model properties should still be specified on the model itself in your OpenWebUI workspace. If you want the most reliable local action execution in this fork, enable **Native Tool Calling** on the OpenWebUI model.

When **Enable Streaming** is on:

1. the integration can stream readable assistant `content`
2. web clients can receive native `tool_calls` and `tool_result`
3. the final short assistant reply still arrives at the end of the run

When **Narrate Streaming Progress** is also on:

1. tool runs emit integration-owned progress sentences before and between tool steps
2. the integration intentionally drives the current Assist streaming path again for experimentation
3. web clients generally handle this better than iOS today
4. current iOS main app may append the streamed final text and then append the `intent-end` final text again
5. stable tool execution remains the default when this option is off

When **Show Structured Tool Details** is on:

1. native tool-call entries are stored separately
2. tool-result entries are stored separately
3. current web clients can show deeper action detail than iOS today

## Gateway-First NaBu Resolution

For NaBu/OpenWebUI setups, the recommended architecture is now:

1. OpenWebUI sends the large NaBu system prompt
2. the MLX/OpenAI-compatible gateway parses `Home Layout`
3. the gateway rewrites tool arguments to include `entity_ids`
4. the Home Assistant fork executes those `entity_ids` directly

This keeps NaBu-specific room mappings close to the model/workspace layer instead of hardcoding them into the Home Assistant integration.

#### Search Configuration
Options related to performing a web search with OpenWebUI. The agent will perform a web search through OpenWebUI and have the model summarize the results.

| Option                        | Description                                                                                                                                                                                                                                                                           |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Search Enabled                | Whether or not the conversation agent should perform web searches when the given sentences are triggered.                                                                                                                                                                             |
| Search Trigger Sentences      | Sentence triggers that tell the conversation agent to search the web for something. One sentence per line. These sentences use the same syntax as Home Assistant's standard trigger sentences, but must contain `{query}` once in each sentence. Some default sentences are provided. |
| Search Results Message Prefix | Text prepended to the search response that indicates a search was performed. A default prefix is provided.                                                                                                                                                                            |

To enable web search in OpenWebUI, see [OpenWebUI's documentation on Web Search][openwebui-search].

## Attributions:
This integration is based on the [hass-ollama-conversation][hass-ollama-conversation] repo.

***

[openwebui]: https://openwebui.com/
[sentence-trigger]: https://www.home-assistant.io/docs/automation/trigger/#sentence-trigger
[hass-ollama-conversation]: https://github.com/ej52/hass-ollama-conversation/
[fallback-conversation-agent]: https://github.com/m50/ha-fallback-conversation
[lang-codes]: https://developers.home-assistant.io/docs/voice/intent-recognition/supported-languages/
[openwebui-search]: https://docs.openwebui.com/features/web_search
