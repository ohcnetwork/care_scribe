from typing import Any

import environ
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from rest_framework.settings import perform_import
from django.dispatch import receiver

from care_scribe.apps import PLUGIN_NAME

env = environ.Env()


class PluginSettings:  # pragma: no cover
    """
    A settings object that allows plugin settings to be accessed as
    properties. For example:

        from plugin.settings import plugin_settings
        print(plugin_settings.API_KEY)

    Any setting with string import paths will be automatically resolved
    and return the class, rather than the string literal.

    """

    def __init__(
        self,
        plugin_name: str = None,
        defaults: dict | None = None,
        import_strings: set | None = None,
        required_settings: set | None = None,
    ) -> None:
        if not plugin_name:
            raise ValueError("Plugin name must be provided")
        self.plugin_name = plugin_name
        self.defaults = defaults or {}
        self.import_strings = import_strings or set()
        self.required_settings = required_settings or set()
        self._cached_attrs = set()
        self.validate()

    def __getattr__(self, attr) -> Any:
        if attr not in self.defaults:
            raise AttributeError("Invalid setting: '%s'" % attr)

        # Try to find the setting from user settings, then from environment variables
        val = self.defaults[attr]
        try:
            val = self.user_settings[attr]
        except KeyError:
            try:
                val = env(attr, cast=type(val))
            except environ.ImproperlyConfigured:
                # Fall back to defaults
                pass

        # Coerce import strings into classes
        if attr in self.import_strings:
            val = perform_import(val, attr)

        self._cached_attrs.add(attr)
        setattr(self, attr, val)
        return val

    @property
    def user_settings(self) -> dict:
        if not hasattr(self, "_user_settings"):
            self._user_settings = getattr(settings, "PLUGIN_CONFIGS", {}).get(
                self.plugin_name, {}
            )
        return self._user_settings

    def validate(self) -> None:
        """
        This method handles the validation of the plugin settings.
        It could be overridden to provide custom validation logic.

        the base implementation checks if all the required settings are truthy.
        """
        for setting in self.required_settings:
            if not getattr(self, setting):
                raise ImproperlyConfigured(
                    f'The "{setting}" setting is required. '
                    f'Please set the "{setting}" in the environment or the {PLUGIN_NAME} plugin config.'
                )

        if getattr(self, "SCRIBE_API_PROVIDER") not in ("openai", "azure", "google"):
            raise ImproperlyConfigured(
                'Invalid value for "SCRIBE_API_PROVIDER". '
                'Please set the "SCRIBE_API_PROVIDER" to "openai", "google" or "azure".'
            )

        if getattr(self, "SCRIBE_API_PROVIDER") == "openai":
            for setting in ("SCRIBE_OPENAI_API_KEY",):
                if not getattr(self, setting):
                    raise ImproperlyConfigured(
                        f'The "{setting}" setting is required when using OpenAI API. '
                        f'Please set the "{setting}" in the environment or the {PLUGIN_NAME} plugin config.'
                    )

        if getattr(self, "SCRIBE_API_PROVIDER") == "azure":
            for setting in ("SCRIBE_AZURE_API_VERSION", "SCRIBE_AZURE_ENDPOINT", "SCRIBE_AZURE_API_KEY"):
                if not getattr(self, setting):
                    raise ImproperlyConfigured(
                        f'The "{setting}" setting is required when using Azure API. '
                        f'Please set the "{setting}" in the environment or the {PLUGIN_NAME} plugin config.'
                    )

        if getattr(self, "SCRIBE_API_PROVIDER") == "google":
            for setting in ("SCRIBE_GOOGLE_PROJECT_ID", "SCRIBE_GOOGLE_LOCATION"):
                if not getattr(self, setting):
                    raise ImproperlyConfigured(
                        f'The "{setting}" setting is required when using Google API. '
                        f'Please set the "{setting}" in the environment or the {PLUGIN_NAME} plugin config.'
                    )

    def reload(self) -> None:
        """
        Deletes the cached attributes so they will be recomputed next time they are accessed.
        """
        for attr in self._cached_attrs:
            delattr(self, attr)
        self._cached_attrs.clear()
        if hasattr(self, "_user_settings"):
            delattr(self, "_user_settings")


REQUIRED_SETTINGS = {
    "SCRIBE_CHAT_MODEL_NAME",
    "SCRIBE_API_PROVIDER",
}

DEFAULTS = {
    "SCRIBE_OPENAI_API_KEY": "",
    "SCRIBE_AZURE_API_KEY": "",
    "SCRIBE_AUDIO_MODEL_NAME": "whisper-1",
    "SCRIBE_CHAT_MODEL_NAME": "gpt-4o",
    "SCRIBE_API_PROVIDER": "openai",
    "SCRIBE_AZURE_API_VERSION": "",
    "SCRIBE_AZURE_ENDPOINT": "",
    "SCRIBE_GOOGLE_PROJECT_ID" : "",
    "SCRIBE_GOOGLE_LOCATION" : "",
    "SCRIBE_TNC": "<ol><li><strong>Data Storage and Privacy:</strong> All patient data will be stored on state-owned cloud infrastructure managed by the Health Department.</li><li><strong>User Responsibility:</strong> CARE Scribe is a supportive data entry tool. All transcriptions must be solely reviewed and confirmed by the attending doctor or nurse. eGov will not, and does not undertake any responsibility or liability to review and confirm the transcripts of the audio data entered into the tool, and shall bear no liability for errors arising from unverified AI-generated content.</li><li><strong>Access Control:</strong> Access to CARE Scribe (including for use of the tool and the transcripts) will be limited to authorized users via secure, role-based authentication, which shall be the responsibility of the Health Department. All usage will be subject to periodic audit and monitoring.</li><li><strong>Legal and Security Compliance:</strong> All data processing will be fully compliant with applicable data protection laws.</li><li><strong>Third-party Service Dependency:</strong> CARE Scribe relies on third-party AI APIs for transcription. eGov does not provide any warranties regarding the same, and will not be liable for service disruptions, inaccuracies, or changes originating from these external providers.</li></ol>"
}

plugin_settings = PluginSettings(
    PLUGIN_NAME, defaults=DEFAULTS, required_settings=REQUIRED_SETTINGS
)


@receiver(setting_changed)
def reload_plugin_settings(*args, **kwargs) -> None:
    setting = kwargs["setting"]
    if setting == "PLUGIN_CONFIGS":
        plugin_settings.reload()
