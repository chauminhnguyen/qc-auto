"""Compatibility entrypoint for the browser capture workflow."""

import asyncio
import sys

from capture_app.workflows.capture import *
from capture_app.workflows.capture import main


if __name__ == "__main__":
    if sys.argv[1:2] == ["init"]:
        from capture_app.reporting.flow_diagram import init_visualization_cli

        raise SystemExit(init_visualization_cli(sys.argv[2:]))
    asyncio.run(main())
