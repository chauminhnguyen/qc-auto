"""
Real `live_snapshot_fn`, built directly on ui_crawler.py's HeadlessCrawler
snapshot logic (SNAPSHOT_JS via _snapshot()) instead of a stand-in.

Opens ONE Playwright browser/page for the whole exploration run -- not one
per screen, since launching a browser per URL would make the diff-engine
pass alone take longer than the rest of the pipeline -- and reuses it for
every distinct page_url the standard-flow session visited.

Reaches into HeadlessCrawler's underscore-prefixed helpers (_snapshot,
_goto_resilient, _settle) by design, so this never drifts out of sync with
SNAPSHOT_JS. If you'd rather not do that, promote those three to public
methods on HeadlessCrawler and drop the underscores here to match.
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright

from capture_app.crawling.ui_crawler import HeadlessCrawler
from capture_app.storage.vault import VaultWriter


class LiveSnapshotter:
    """
    Context manager providing a `snapshot(url) -> (url, items)` method
    compatible with vault_adapter.taken_actions_for_session()'s
    `live_snapshot_fn` parameter.

    Usage:
        with LiveSnapshotter(vault) as live:
            snapshots, taken = taken_actions_for_session(
                vault, session_id, live.snapshot
            )
    """

    def __init__(self, vault: VaultWriter):
        # A bare HeadlessCrawler instance, used only for its _snapshot()/
        # _goto_resilient()/_settle() helpers -- never call .crawl() or
        # .seed() on it, it isn't seeded and isn't meant to explore
        # anything on its own.
        self._crawler = HeadlessCrawler(vault=vault, verbose=False)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self) -> "LiveSnapshotter":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    def snapshot(self, url: str):
        """live_snapshot_fn(url) -> (url, list[Interactable])."""
        self._crawler._goto_resilient(self._page, url)
        self._crawler._settle(self._page)
        items = self._crawler._snapshot(self._page)
        return self._page.url, items
