"""Run offline tests for one service or all services."""
from __future__ import annotations

import argparse

from tests.test_services import SERVICE_TESTS, run_tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "service",
        nargs="?",
        choices=["all", *SERVICE_TESTS],
        default="all",
    )
    return run_tests(parser.parse_args().service)


if __name__ == "__main__":
    raise SystemExit(main())
