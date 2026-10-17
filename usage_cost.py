"""Compatibility entrypoint for chat usage and cost reporting."""

from capture_app.chat.usage_cost import *
from capture_app.chat.usage_cost import main


if __name__ == "__main__":
    raise SystemExit(main())
