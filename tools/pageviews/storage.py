import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from . import SCHEMA_VERSION, __version__
from .errors import PageviewsError
from .models import PageviewsRequest, RawResponse, build_request
from .validation import ValidatedSeries, validate_response


def _json_bytes(value: object) -> bytes:
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    )
    return (text + "\n").encode("utf-8")


def _request_directory(root: Path, request: PageviewsRequest) -> Path:
    identity = {
        "schema_version": SCHEMA_VERSION,
        "processor_version": __version__,
        "request": request.as_dict(),
    }
    key = hashlib.sha256(_json_bytes(identity)).hexdigest()
    return root.resolve() / key


@dataclass(frozen=True, slots=True)
class Snapshot:
    directory: Path
    response: RawResponse
    request: PageviewsRequest

    def artifact_paths(self) -> dict[str, str]:
        return {
            "raw": str(self.directory / "raw.json"),
            "series": str(self.directory / "series.json"),
            "metadata": str(self.directory / "metadata.json"),
        }


def _publish_latest(directory: Path, snapshot: str, metadata: bytes) -> None:
    pointer = {
        "snapshot": snapshot,
        "metadata_sha256": hashlib.sha256(metadata).hexdigest(),
    }
    temporary = directory / f".latest-{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(_json_bytes(pointer))
        temporary.replace(directory / "latest.json")
    finally:
        temporary.unlink(missing_ok=True)


def save_snapshot(
    root: Path,
    request: PageviewsRequest,
    response: RawResponse,
    series: ValidatedSeries,
) -> Snapshot:
    if response.url != request.url:
        raise PageviewsError("storage_error", "Response URL does not match the request.")
    series_bytes = _json_bytes(
        {
            "schema_version": SCHEMA_VERSION,
            "days": [day.as_dict() for day in series.days],
        }
    )
    metadata = _json_bytes(
        {
            "schema_version": SCHEMA_VERSION,
            "processor_version": __version__,
            "request": request.as_dict(),
            "source": response.source_metadata(),
            "status": series.status,
            "coverage": series.coverage_summary(),
            "series_sha256": hashlib.sha256(series_bytes).hexdigest(),
        }
    )
    try:
        directory = _request_directory(root, request)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / uuid4().hex
        with TemporaryDirectory(prefix=".pending-", dir=directory) as temporary:
            staging = Path(temporary)
            (staging / "raw.json").write_bytes(response.body)
            (staging / "series.json").write_bytes(series_bytes)
            (staging / "metadata.json").write_bytes(metadata)
            staging.rename(destination)
        # Publish only after the complete snapshot is in place.
        _publish_latest(directory, destination.name, metadata)
    except OSError as error:
        raise PageviewsError(
            "storage_error",
            "Could not save the snapshot; "
            "check output directory permissions and disk space.",
            details={"exception_type": type(error).__name__},
        ) from error
    return Snapshot(directory=destination, response=response, request=request)


def _read_metadata(
    snapshot_directory: Path, expected_checksum: str | None = None
) -> dict[str, object]:
    metadata_bytes = (snapshot_directory / "metadata.json").read_bytes()
    if (
        expected_checksum is not None
        and hashlib.sha256(metadata_bytes).hexdigest() != expected_checksum
    ):
        raise ValueError("Metadata checksum mismatch.")
    metadata = json.loads(metadata_bytes)
    if not isinstance(metadata, dict):
        raise ValueError("Invalid metadata.")
    if (
        type(metadata.get("schema_version")) is not int
        or metadata.get("schema_version") != SCHEMA_VERSION
        or metadata.get("processor_version") != __version__
    ):
        raise ValueError("Unsupported snapshot version.")
    return metadata


def _required_string(values: dict[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Snapshot field {key} must be a string.")
    return value


def _restore_request(value: object) -> PageviewsRequest:
    if not isinstance(value, dict):
        raise ValueError("Missing snapshot request.")
    window = value.get("requested_window")
    if not isinstance(window, dict):
        raise ValueError("Missing requested window.")
    lag_days = value.get("excluded_recent_days")
    if type(lag_days) is not int:
        raise ValueError("Invalid safety-buffer setting.")
    try:
        return build_request(
            project=_required_string(value, "project"),
            article=_required_string(value, "article"),
            start=_required_string(window, "start"),
            end=_required_string(window, "end"),
            as_of=_required_string(value, "as_of"),
            lag_days=lag_days,
        )
    except PageviewsError as error:
        raise ValueError("Invalid snapshot request parameters.") from error


def _read_snapshot_files(
    snapshot_directory: Path,
    metadata: dict[str, object],
    request: PageviewsRequest,
) -> Snapshot:
    if metadata.get("request") != request.as_dict():
        raise ValueError("Snapshot request mismatch.")
    source = metadata.get("source")
    if not isinstance(source, dict) or source.get("url") != request.url:
        raise ValueError("Cached source mismatch.")
    fetched_at = source.get("fetched_at_utc")
    if not isinstance(fetched_at, str):
        raise ValueError("Missing fetch timestamp.")
    if datetime.fromisoformat(fetched_at).utcoffset() is None:
        raise ValueError("Missing timezone-aware fetch timestamp.")
    if source.get("http_status") != 200:
        raise ValueError("Cached HTTP status is not successful.")
    raw = (snapshot_directory / "raw.json").read_bytes()
    series = (snapshot_directory / "series.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != source.get("response_sha256"):
        raise ValueError("Raw response checksum mismatch.")
    if hashlib.sha256(series).hexdigest() != metadata.get("series_sha256"):
        raise ValueError("Series checksum mismatch.")
    try:
        verified = validate_response(raw, request)
    except PageviewsError as error:
        raise ValueError("The saved raw response failed validation.") from error
    expected_series = {
        "schema_version": SCHEMA_VERSION,
        "days": [day.as_dict() for day in verified.days],
    }
    if _json_bytes(json.loads(series)) != _json_bytes(expected_series):
        raise ValueError("Saved calendar does not match the raw response.")
    if (
        metadata.get("status") != verified.status
        or metadata.get("coverage") != verified.coverage_summary()
    ):
        raise ValueError("Saved coverage does not match the raw response.")
    return Snapshot(
        directory=snapshot_directory,
        response=RawResponse(url=request.url, fetched_at=fetched_at, body=raw),
        request=request,
    )


def _pointer_checksum(pointer: dict[str, object]) -> str:
    checksum = pointer.get("metadata_sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ValueError("Invalid metadata checksum in cache pointer.")
    return checksum


def _read_snapshot(
    directory: Path, pointer: object, request: PageviewsRequest
) -> Snapshot:
    if not isinstance(pointer, dict):
        raise ValueError("Invalid cache pointer.")
    name = pointer.get("snapshot")
    if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{32}", name):
        raise ValueError("Invalid snapshot identifier.")
    snapshot_directory = directory / name
    metadata = _read_metadata(snapshot_directory, _pointer_checksum(pointer))
    return _read_snapshot_files(snapshot_directory, metadata, request)


def _snapshot_metadata_checksum(directory: Path) -> str | None:
    try:
        pointer = json.loads((directory.parent / "latest.json").read_bytes())
    except (OSError, ValueError):
        # Explicit snapshot paths do not require a working cache index.
        return None
    if isinstance(pointer, dict) and pointer.get("snapshot") == directory.name:
        return _pointer_checksum(pointer)
    return None


def read_snapshot(directory: Path) -> Snapshot:
    try:
        directory = directory.expanduser().resolve()
        checksum = _snapshot_metadata_checksum(directory)
        metadata = _read_metadata(directory, checksum)
        request = _restore_request(metadata.get("request"))
        return _read_snapshot_files(directory, metadata, request)
    except (OSError, ValueError) as error:
        raise PageviewsError(
            "snapshot_error",
            "The selected snapshot is missing, unreadable or inconsistent. "
            "Check its path and files, or explicitly download a new snapshot.",
            details={"reason": str(error), "exception_type": type(error).__name__},
        ) from error


def load_snapshot(root: Path, request: PageviewsRequest) -> Snapshot | None:
    try:
        directory = _request_directory(root, request)
        try:
            pointer_bytes = (directory / "latest.json").read_bytes()
        except FileNotFoundError:
            return None
        pointer = json.loads(pointer_bytes)
        return _read_snapshot(directory, pointer, request)
    except (OSError, ValueError) as error:
        raise PageviewsError(
            "cache_error",
            "The saved snapshot is unreadable or inconsistent. Inspect it or use "
            "--refresh to download a new snapshot; existing snapshots are preserved.",
            details={"exception_type": type(error).__name__},
        ) from error