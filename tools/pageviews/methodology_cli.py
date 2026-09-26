from argparse import Namespace

from .cli_common import JsonArgumentParser
from .errors import PageviewsError
from .methodology import MethodologyOptions
from .trend_model import MODEL_NAME


def add_methodology_arguments(parser: JsonArgumentParser) -> None:
    parser.add_argument(
        "--methodology", action="store_true",
        help="Add full-calendar-month year-over-year comparisons and explicit interpretation limits",
    )
    parser.add_argument(
        "--trend-model", choices=(MODEL_NAME,),
        help="Optional conditional slope model; requires --methodology and --hac-lags",
    )
    parser.add_argument(
        "--hac-lags", type=int,
        help="Pre-specified maximum daily HAC lag; no default; zero omits serial-correlation adjustment",
    )


def methodology_options(args: Namespace) -> MethodologyOptions | None:
    if not args.methodology:
        if args.trend_model is not None or args.hac_lags is not None:
            raise PageviewsError("invalid_arguments", "Trend-model parameters require --methodology.")
        return None
    return MethodologyOptions(trend_model=args.trend_model, hac_lags=args.hac_lags)