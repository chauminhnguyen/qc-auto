"""Compatibility launcher for the crawler-fix maintenance script."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    script = Path(__file__).resolve().parent / "tools" / "merge_crawler_fixes.py"
    runpy.run_path(str(script), run_name="__main__")
