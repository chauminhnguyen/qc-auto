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

    def __init__(self, vault: VaultWriter, auth_state=None):
        # A bare HeadlessCrawler instance, used only for its _snapshot()/
        # _goto_resilient()/_settle() helpers -- never call .crawl() or
        # .seed() on it, it isn't seeded and isn't meant to explore
        # anything on its own.
        self._crawler = HeadlessCrawler(vault=vault, verbose=False, auth_state=auth_state)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._gate = None

    def __enter__(self) -> "LiveSnapshotter":
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
            from capture_app.env import load_project_env
            load_project_env()
            self._context = self._crawler._new_safe_context(self._browser, "")
            self._gate = self._crawler.safety_gate
            self._page = self._context.new_page()
            self._page.set_default_timeout(5000)
            self._page.set_default_navigation_timeout(10000)
            return self
        except Exception:
            if self._browser is not None:
                self._browser.close()
            self._pw.stop()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            try:
                if self._gate is not None:
                    self._gate.close()
                    from capture_app.safety_gate.review import review_if_interactive
                    review_if_interactive(str(self._gate.decisions.path))
            finally:
                if self._pw is not None:
                    self._pw.stop()

    def snapshot(self, url: str):
        """live_snapshot_fn(url) -> (url, list[Interactable])."""
        if self._gate is not None:
            self._gate.set_trigger(self._page.url or url, "Snapshot page")
        self._crawler._goto_resilient(self._page, url)
        self._crawler._settle(self._page)
        items = self._crawler._snapshot(self._page)
        if self._gate is not None:
            self._gate.checkpoint("Snapshot page", self._page.url, "ok")
        return self._page.url, items
