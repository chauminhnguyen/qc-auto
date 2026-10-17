"""Compatibility entrypoint for parameter reports."""

import sys

from capture_app.reporting.param_report import *
from capture_app.reporting.param_report import main


if __name__ == "__main__":
    sys.exit(main())