import argparse
import json

from . import SCHEMA_VERSION
from .errors import PageviewsError


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PageviewsError("invalid_arguments", message)


def print_result(result: dict[str, object]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


def print_error(error: PageviewsError) -> int:
    print_result(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "error": error.as_dict(),
        }
    )
    return 2 if error.code in {"invalid_arguments", "invalid_request"} else 1