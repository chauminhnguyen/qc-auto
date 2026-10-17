"""QC knowledge ingestion alongside the capture vault.

This package owns only the ``Knowledge/`` subtree. The capture index is opened
read-only and crawler-owned notes are never written by this package.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
