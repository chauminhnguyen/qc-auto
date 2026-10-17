"""Compatibility entrypoint for vault enrichment."""

from capture_app.workflows.enrich import *
from capture_app.workflows.enrich import main


if __name__ == "__main__":
    main()