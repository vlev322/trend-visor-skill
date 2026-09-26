import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from tools.pageviews.errors import PageviewsError

ENV_PREFIX = "TREND_VISOR_LLM_"


@dataclass(frozen=True, slots=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str) or not self.api_key
            or self.api_key == "replace_with_your_api_key"
            or any(not 33 <= ord(char) <= 126 for char in self.api_key)
        ):
            raise PageviewsError(
                "model_not_configured",
                "Set TREND_VISOR_LLM_API_KEY locally in .env or the environment. "
                "Do not put keys in command arguments or reports.",
            )
        if not isinstance(self.base_url, str) or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.base_url
        ):
            raise PageviewsError("invalid_request", "The model base URL must be an HTTPS URL.")
        try:
            url = urlsplit(self.base_url)
            port = url.port
        except ValueError as error:
            raise PageviewsError("invalid_request", "Invalid model base URL.") from error
        if (
            url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or any(char in self.base_url for char in "?#\\")
            or (port is not None and port <= 0)
        ):
            raise PageviewsError(
                "invalid_request", "Use an HTTPS model base URL without credentials, query or fragment."
            )
        if (
            not isinstance(self.model, str) or not 1 <= len(self.model) <= 200
            or any(not 33 <= ord(char) <= 126 for char in self.model)
        ):
            raise PageviewsError("invalid_request", "Provide an explicit model ID without whitespace.")
        if self.api_key in self.base_url or self.api_key in self.model:
            raise PageviewsError("invalid_request", "Credentials cannot appear in provider or model metadata.")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


def load_config(path: Path, *, model: str | None = None) -> ModelConfig:
    values = {}
    try:
        with path.expanduser().open("rb") as file:
            body = file.read(65537)
        if len(body) > 65536:
            raise PageviewsError("model_not_configured", "The selected .env file exceeds 64 KiB.")
        body = body.decode("utf-8")
    except FileNotFoundError:
        body = None
    except (OSError, ValueError, RuntimeError) as error:
        raise PageviewsError("model_not_configured", "Could not read the selected local .env file.") from error
    if body is not None:
        try:
            from dotenv import dotenv_values
        except ImportError as error:
            raise PageviewsError(
                "missing_dependency", "Install the evaluation extra using uv sync --locked --all-extras."
            ) from error
        values = dotenv_values(stream=io.StringIO(body), interpolate=False)
    config = {
        name: os.environ.get(ENV_PREFIX + name, values.get(ENV_PREFIX + name))
        for name in ("BASE_URL", "MODEL", "API_KEY")
    }
    return ModelConfig(config["BASE_URL"], model if model is not None else config["MODEL"], config["API_KEY"])