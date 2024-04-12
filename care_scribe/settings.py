import environ
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.settings import perform_import

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
        plugin_name=None,
        user_settings=None,
        defaults=None,
        import_strings=None,
        required_settings=None,
    ):
        if not plugin_name:
            raise ValueError("Plugin name must be provided")
        self.plugin_name = plugin_name
        if user_settings:
            self._user_settings = user_settings
        else:
            self._user_settings = getattr(settings, "PLUGIN_CONFIGS", {}).get(
                self.plugin_name, {}
            )
        self.defaults = defaults or {}
        self.import_strings = import_strings or []
        self.required_settings = required_settings or []
        self._cached_attrs = set()
        self.validate()

    def __getattr__(self, attr):
        if attr not in self.defaults:
            raise AttributeError("Invalid setting: '%s'" % attr)
        

        val = self.defaults[attr]
        try:
            val = env.get_value(attr, cast=type(val))
        except environ.ImproperlyConfigured:
            try:
                val = self.user_settings[attr]
            except KeyError:
                pass

        # Coerce import strings into classes
        if attr in self.import_strings:
            val = perform_import(val, attr)

        self._cached_attrs.add(attr)
        setattr(self, attr, val)
        return val

    @property
    def user_settings(self):
        if not hasattr(self, "_user_settings"):
            self._user_settings = getattr(settings, "PLUGIN_CONFIGS", {}).get(
                self.plugin_name, {}
            )
        return self._user_settings

    def validate(self):
        for setting in self.required_settings:
            if not getattr(self, setting):
                raise ImproperlyConfigured(
                    f'The "{setting}" setting is required. '
                    f'Please set the "{setting}" in the environment or the {PLUGIN_NAME} plugin config.'
                )


TSP_API_KEY = "TRANSCRIBE_SERVICE_PROVIDER_API_KEY"

REQUIRED_SETTINGS = [
    TSP_API_KEY,
]

DEFAULTS = {TSP_API_KEY: "test"}

plugin_settings = PluginSettings(
    PLUGIN_NAME, defaults=DEFAULTS, required_settings=REQUIRED_SETTINGS
)
