[![GitHub Release](https://img.shields.io/github/release/TheRealPSV/ha-openwebui-conversation.svg?style=flat-square)](https://github.com/TheRealPSV/ha-openwebui-conversation/releases)
[![Downloads](https://img.shields.io/github/downloads/TheRealPSV/ha-openwebui-conversation/total?style=flat-square)](https://github.com/TheRealPSV/ha-openwebui-conversation/releases)
[![Build Status](https://img.shields.io/github/actions/workflow/status/TheRealPSV/ha-openwebui-conversation/validate.yml?style=flat-square)](https://github.com/TheRealPSV/ha-openwebui-conversation/actions/workflows/validate.yaml)
[![License](https://img.shields.io/github/license/TheRealPSV/ha-openwebui-conversation.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-default-orange.svg?style=flat-square)](https://hacs.xyz)

# OpenWebUI Conversation

The OpenWebUI integration adds a conversation agent powered by [OpenWebUI][openwebui] in Home Assistant.

This conversation agent is unable to control your house. The OpenWebUI conversation agent can be used in automations, but not as a [sentence trigger][sentence-trigger]. It can only query information that has been provided by Home Assistant. To be able to answer questions about your house, Home Assistant will need to provide OpenWebUI with the details of your house, which include areas, devices and their states.

## Installation

To install the __OpenWebUI Conversation__ integration to your Home Assistant instance, use this My button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=therealpsv&repository=ha-openwebui-conversation&category=integration)

#### Manual Installation
If the above My button doesn’t work, you can also perform the following steps manually:

* Browse to your Home Assistant instance.
* Go to HACS > Integrations > Custom Repositories.
* Add custom repository.
  * Repository is `TheRealPSV/ha-openwebui-conversation`.
  * Category is `Integration`.
* Click ___Explore & Download Repositories___.
* From the list, select OpenWebUI Conversation.
* In the bottom right corner, click the ___Download___ button.
* Follow the instructions on screen to complete the installation.

#### Note:
HACS does not "configure" the integration for you, You must add OpenWebUI Conversation after installing via HACS.

* Browse to your Home Assistant instance.
* Go to Settings > Devices & Services.
* In the bottom right corner, select the ___Add Integration___ button.
* From the list, select OpenWebUI Conversation.
* Follow the instructions on screen to complete the setup.

## Options
Options for OpenWebUI Conversation can be set via the user interface, by taking the following steps:

* Browse to your Home Assistant instance.
* Go to Settings > Devices & Services.
* If multiple instances of OpenWebUI Conversation are configured, choose the instance you want to configure.
* Select the integration, then select ___Configure___.

#### General Settings
Settings relating to the integration itself.

| Option      | Description                                                               |
| ----------- | ------------------------------------------------------------------------- |
| API Timeout | The maximum amount of time to wait for a response from the API in seconds |

#### Model Configuration
The language model and additional parameters to fine tune the responses.

| Option | Description                          |
| ------ | ------------------------------------ |
| Model  | The model used to generate response. |

## Attributions:
This integration is based on the [hass-ollama-conversation][hass-ollama-conversation] repo.

***

[openwebui]: https://openwebui.com/
[sentence-trigger]: https://www.home-assistant.io/docs/automation/trigger/#sentence-trigger
[hass-ollama-conversation]: https://github.com/ej52/hass-ollama-conversation/
