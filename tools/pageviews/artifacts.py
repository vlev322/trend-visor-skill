import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import PageviewsError

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class JsonArtifact:
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class TextArtifact:
    path: Path
    sha256: str


def json_output_path(path: Path) -> Path:
    return artifact_output_path(path, ".json")


def artifact_output_path(path: Path, suffix: str) -> Path:
    try:
        return _new_path(path, suffix)
    except PageviewsError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise PageviewsError(
            "artifact_write_error", "Could not inspect the output path.",
            details={"exception_type": type(error).__name__},
        ) from error


def _new_path(path: Path, suffix: str) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise PageviewsError("output_exists", "Choose a new path, not an output symlink.")
    path = path.resolve()
    if suffix not in {".json", ".md"} or path.suffix.lower() != suffix:
        raise PageviewsError("invalid_request", f"The output must have a {suffix} extension.")
    if path.exists():
        raise PageviewsError("output_exists", "Output already exists; choose a new filename.")
    for parent in path.parents:
        if all((parent / name).is_file() for name in ("raw.json", "series.json", "metadata.json")):
            raise PageviewsError("invalid_request", "Save results outside snapshot directories.")
    return path


def _publish_bytes(body: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".artifact-", dir=path.parent) as temporary:
        prepared = Path(temporary) / "result"
        prepared.write_bytes(body)
        # Publish complete bytes without replacing an existing destination.
        os.link(prepared, path)


def save_json_artifact(value: dict[str, object], path: Path) -> JsonArtifact:
    try:
        path = json_output_path(path)
        body = (json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ) + "\n").encode("utf-8")
        if len(body) > MAX_ARTIFACT_BYTES:
            raise PageviewsError("artifact_too_large", "JSON result exceeds 10 MiB.")
        _publish_bytes(body, path)
    except FileExistsError as error:
        raise PageviewsError("output_exists", "Output already exists; choose a new filename.") from error
    except OSError as error:
        raise PageviewsError(
            "artifact_write_error", "Could not save JSON; check path, permissions and disk space.",
            details={"exception_type": type(error).__name__},
        ) from error
    except (TypeError, ValueError, RecursionError) as error:
        raise PageviewsError(
            "invalid_request", "The result must contain finite, UTF-8-encodable JSON values.",
            details={"exception_type": type(error).__name__},
        ) from error
    return JsonArtifact(path=path, sha256=hashlib.sha256(body).hexdigest())


def save_markdown_artifact(text: str, path: Path) -> TextArtifact:
    try:
        path = artifact_output_path(path, ".md")
        if not isinstance(text, str):
            raise PageviewsError("invalid_request", "Markdown output must be text.")
        body = text.encode("utf-8")
        if len(body) > MAX_ARTIFACT_BYTES:
            raise PageviewsError("artifact_too_large", "Markdown result exceeds 10 MiB.")
        _publish_bytes(body, path)
    except FileExistsError as error:
        raise PageviewsError("output_exists", "Output already exists; choose a new filename.") from error
    except OSError as error:
        raise PageviewsError("artifact_write_error", "Could not save Markdown; check path, permissions and disk space.",
                             details={"exception_type": type(error).__name__}) from error
    except UnicodeError as error:
        raise PageviewsError("invalid_request", "Markdown output must be UTF-8 encodable.") from error
    return TextArtifact(path, hashlib.sha256(body).hexdigest())