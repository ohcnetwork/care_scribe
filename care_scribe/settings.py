from typing import Any

from django.conf import settings
from django.test.signals import setting_changed
from rest_framework.settings import APISettings

from care_scribe.apps import PLUGIN_NAME


USER_SETTINGS = getattr(settings, "PLUGIN_CONFIGS", {}).get(PLUGIN_NAME, {})

DEFAULTS = {
    "TRANSCRIBE_SERVICE_PROVIDER_API_KEY": None,
}

IMPORT_STRINGS = []


class PluginSettings(APISettings):  # pragma: no cover
    def __check_user_settings(self, user_settings: dict[str, Any]) -> dict[str, Any]:
        
        if (
            "TRANSCRIBE_SERVICE_PROVIDER_API_KEY" not in user_settings
            or not user_settings.get("TRANSCRIBE_SERVICE_PROVIDER_API_KEY")
        ):
            raise RuntimeError(
                'The "TRANSCRIBE_SERVICE_PROVIDER_API_KEY" setting is required. '
                f'Please set the "TRANSCRIBE_SERVICE_PROVIDER_API_KEY" in {PLUGIN_NAME} plugin config.'
            )
        return user_settings


plugin_settings = PluginSettings(USER_SETTINGS, DEFAULTS, IMPORT_STRINGS)


def reload_plugin_settings(*args, **kwargs) -> None:  # pragma: no cover
    global plugin_settings

    setting, value = kwargs["setting"], kwargs["value"]

    if setting == "PLUGIN_CONFIGS":
        plugin_settings = PluginSettings(
            value.get(PLUGIN_NAME, {}), DEFAULTS, IMPORT_STRINGS
        )


setting_changed.connect(reload_plugin_settings)
