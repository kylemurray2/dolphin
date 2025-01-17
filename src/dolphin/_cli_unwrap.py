#!/usr/bin/env python
import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    _SubparserType = argparse._SubParsersAction[argparse.ArgumentParser]
else:
    _SubparserType = Any


def get_parser(subparser=None, subcommand_name="unwrap") -> argparse.ArgumentParser:
    """Set up the command line interface."""
    metadata = dict(
        description="Create a configuration file for a displacement workflow.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        # https://docs.python.org/3/library/argparse.html#fromfile-prefix-chars
        fromfile_prefix_chars="@",
    )
    if subparser:
        # Used by the subparser to make a nested command line interface
        parser = subparser.add_parser(subcommand_name, **metadata)
    else:
        parser = argparse.ArgumentParser(**metadata)  # type: ignore

    # parser._action_groups.pop()
    parser.add_argument(
        "-o",
        "--output-path",
        default=Path("."),
        help="Path to output directory to store results",
    )
    # Get Inputs from the command line
    inputs = parser.add_argument_group("Input options")
    inputs.add_argument(
        "--ifg-filenames",
        nargs=argparse.ZERO_OR_MORE,
        help=(
            "List the paths of all ifg files to include. Can pass a newline delimited"
            " file with @ifg_filelist.txt"
        ),
    )
    inputs.add_argument(
        "--cor-filenames",
        nargs=argparse.ZERO_OR_MORE,
        help=(
            "List the paths of all ifg files to include. Can pass a newline delimited"
            " file with @cor_filelist.txt"
        ),
    )
    inputs.add_argument(
        "--mask-filename",
        help=(
            "Path to Byte mask file used to ignore low correlation/bad data (e.g water"
            " mask). Convention is 0 for no data/invalid, and 1 for good data."
        ),
    )
    parser.add_argument(
        "--nlooks",
        type=int,
        help="Effective number of looks used to form correlation",
    )

    parser.add_argument(
        "--max-jobs",
        type=int,
        default=1,
        help="Number of parallel files to unwrap",
    )
    # Add ability for downsampling/running only coarse_unwrap
    parser.add_argument(
        "--downsample-factor",
        type=int,
        default=1,
        help=(
            "Running coarse_unwrap: Downsample the interferograms by this factor to"
            " unwrap faster."
        ),
    )
    parser.set_defaults(run_func=_run_unwrap)

    return parser


def _run_unwrap(*args, **kwargs):
    """Run `dolphin.unwrap.run`.

    Wrapper for the dolphin.unwrap to delay import time.
    """
    from dolphin import unwrap

    return unwrap.run(*args, **kwargs)


def main(args=None):
    """Get the command line arguments and unwrap files."""
    from dolphin import unwrap

    parser = get_parser()
    parsed_args = parser.parse_args(args)
    unwrap.run(**vars(parsed_args))


if __name__ == "__main__":
    main()
