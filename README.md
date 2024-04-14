# Care Scribe

[![Release Status](https://img.shields.io/pypi/v/care_scribe.svg)](https://pypi.python.org/pypi/care_scribe)
[![Build Status](https://github.com/coronasafe/care_scribe/actions/workflows/build.yaml/badge.svg)](https://github.com/coronasafe/care_scribe/actions/workflows/build.yaml)

Care Scribe is a plugin for care to add voice auto fill support using external services like OpenAI whisper and Google Speech to Text.


## Features

- Voice auto fill support for care
- Support for OpenAI whisper and Google Speech to Text

## Installation

https://care-be-docs.coronasafe.network/pluggable-apps/configuration.html

https://github.com/coronasafe/care/blob/develop/plug_config.py


To install care scribe, you can add the plugin config in [care/plug_config.py](https://github.com/coronasafe/care/blob/develop/plug_config.py) as follows:

```python
...

scribe_plug = Plug(
    name="care_scribe",
    package_name="git+https://github.com/coronasafe/care_scribe.git",
    version="@master",
    configs={
        "TRANSCRIBE_SERVICE_PROVIDER_API_KEY": "secret",
    },
)
plugs = [scribe_plug]
...
```

## Configuration

The following configurations variables are available for Care Scribe:

- `TRANSCRIBE_SERVICE_PROVIDER_API_KEY`: API key for the transcribe service provider (OpenAI whisper or Google Speech to Text)

The plugin will try to find the API key from the config first and then from the environment variable.

## License

This project is licensed under the terms of the [MIT license](LICENSE).


---
This plugin was created with [Cookiecutter](https://github.com/audreyr/cookiecutter) using the [coronasafe/care-plugin-cookiecutter](https://github.com/coronasafe/care-plugin-cookiecutter).