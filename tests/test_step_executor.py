import unittest
from dataclasses import replace

from capture_app.crawling.step_executor import (
    PreparedStep,
    StepExecutionError,
    execute_step,
    prepare_step,
)


class FakeLocator:
    def __init__(self, count=1, destinations=None, selector=""):
        self.match_count = count
        self.destinations = destinations or []
        self.selector = selector
        self.selected_index = None
        self.calls = []

    def count(self):
        return self.match_count

    def evaluate_all(self, _expression):
        return self.destinations

    def nth(self, index):
        self.selected_index = index
        return self

    def click(self, **kwargs):
        self.calls.append(("click", kwargs))

    def fill(self, value, **kwargs):
        self.calls.append(("fill", value, kwargs))

    def check(self, **kwargs):
        self.calls.append(("check", kwargs))

    def uncheck(self, **kwargs):
        self.calls.append(("uncheck", kwargs))

    def select_option(self, *args, **kwargs):
        self.calls.append(("select_option", args, kwargs))


class FakePage:
    def __init__(
        self,
        selector_count=0,
        role_count=1,
        locator_counts=None,
        locator_destinations=None,
    ):
        self.locator_counts = locator_counts or {}
        self.locator_destinations = locator_destinations or {}
        self.selector_locator = FakeLocator(selector_count)
        self.role_locator = FakeLocator(role_count)
        self.role_calls = []
        self.locators = []

    def locator(self, selector):
        if selector not in self.locator_counts and selector not in self.locator_destinations:
            self.selector_locator.selector = selector
            return self.selector_locator
        locator = FakeLocator(
            self.locator_counts.get(selector, self.selector_locator.match_count),
            self.locator_destinations.get(selector, []),
            selector,
        )
        self.locators.append(locator)
        return locator

    def get_by_role(self, role, **kwargs):
        self.role_calls.append((role, kwargs))
        return self.role_locator


class StepExecutorTests(unittest.TestCase):
    def test_ambiguous_accessible_target_fails_instead_of_choosing_first(self):
        prepared = prepare_step({
            "seq": 3,
            "step_type": "tap",
            "label": "Add to cart",
            "role": "button",
        })
        page = FakePage(role_count=2)

        with self.assertRaises(StepExecutionError) as raised:
            execute_step(page, prepared)

        self.assertEqual(raised.exception.kind, "ambiguous_target")
        self.assertEqual(page.role_calls[0][0], "button")

    def test_unique_captured_selector_is_used_before_accessible_name(self):
        prepared = prepare_step({
            "seq": 1,
            "step_type": "tap",
            "selector": "#continue",
            "label": "Continue",
            "role": "button",
        })
        page = FakePage(selector_count=1, role_count=4)

        execute_step(page, prepared)

        self.assertEqual(page.selector_locator.calls[0][0], "click")
        self.assertEqual(page.role_calls, [])

    def test_unique_captured_selector_can_identify_unlabeled_tap(self):
        prepared = prepare_step({
            "seq": 2,
            "step_type": "tap",
            "selector": "a.product-image",
            "label": "",
            "tag": "a",
        })
        page = FakePage(selector_count=1, role_count=0)

        execute_step(page, prepared)

        self.assertEqual(page.selector_locator.calls[0][0], "click")
        self.assertEqual(page.role_calls, [])

    def test_positional_selector_uses_expected_destination_to_find_order_row(self):
        selector = "div.order:nth-of-type(1) > input.details"
        unpositioned = "div.order > input.details"
        prepared = prepare_step({
            "seq": 3,
            "step_type": "tap",
            "selector": selector,
            "label": "Details",
            "tag": "input",
        })
        prepared = replace(
            prepared,
            expected_navigation_url="https://shop.example.test/orderdetails/42",
        )
        page = FakePage(
            selector_count=1,
            locator_counts={selector: 1, unpositioned: 2},
            locator_destinations={
                selector: ["setLocation('/orderdetails/43')"],
                unpositioned: [
                    "setLocation('/orderdetails/43')",
                    "setLocation('/orderdetails/42')",
                ],
            },
        )

        execute_step(page, prepared)

        candidates = next(locator for locator in page.locators if locator.selector == unpositioned)
        self.assertEqual(candidates.selected_index, 1)
        self.assertEqual(candidates.calls[0][0], "click")

    def test_unique_captured_selector_wins_when_it_matches_expected_destination(self):
        selector = "li:nth-of-type(4) > a.ico-wishlist"
        unpositioned = "li > a.ico-wishlist"
        prepared = prepare_step({
            "seq": 3,
            "step_type": "tap",
            "selector": selector,
            "label": "Wishlist",
            "tag": "a",
        })
        prepared = replace(
            prepared,
            expected_navigation_url="https://shop.example.test/wishlist",
        )
        page = FakePage(
            selector_count=1,
            locator_counts={selector: 1, unpositioned: 2},
            locator_destinations={
                selector: ["https://shop.example.test/wishlist"],
                unpositioned: [
                    "https://shop.example.test/wishlist",
                    "https://shop.example.test/wishlist",
                ],
            },
        )

        execute_step(page, prepared)

        captured = next(locator for locator in page.locators if locator.selector == selector)
        self.assertEqual(captured.calls[0][0], "click")
        self.assertFalse(any(locator.selector == unpositioned for locator in page.locators))

    def test_positional_selector_does_not_guess_when_destination_is_missing(self):
        selector = "div.order:nth-of-type(1) > input.details"
        unpositioned = "div.order > input.details"
        prepared = prepare_step({
            "seq": 3,
            "step_type": "tap",
            "selector": selector,
            "label": "Details",
            "tag": "input",
        })
        prepared = replace(
            prepared,
            expected_navigation_url="https://shop.example.test/orderdetails/42",
        )
        page = FakePage(
            selector_count=1,
            locator_counts={selector: 1, unpositioned: 2},
            locator_destinations={
                selector: ["setLocation('/orderdetails/43')"],
                unpositioned: ["setLocation('/orderdetails/43')"] * 2,
            },
        )

        with self.assertRaises(StepExecutionError) as raised:
            execute_step(page, prepared)

        self.assertEqual(raised.exception.kind, "target_not_found")

    def test_positional_selector_fails_when_multiple_rows_match_destination(self):
        selector = "div.order:nth-of-type(1) > input.details"
        unpositioned = "div.order > input.details"
        prepared = prepare_step({
            "seq": 3,
            "step_type": "tap",
            "selector": selector,
            "label": "Details",
            "tag": "input",
        })
        prepared = replace(
            prepared,
            expected_navigation_url="https://shop.example.test/orderdetails/42",
        )
        page = FakePage(
            selector_count=1,
            locator_counts={unpositioned: 2},
            locator_destinations={
                unpositioned: ["setLocation('/orderdetails/42')"] * 2,
            },
        )

        with self.assertRaises(StepExecutionError) as raised:
            execute_step(page, prepared)

        self.assertEqual(raised.exception.kind, "ambiguous_target")

    def test_unlabeled_tap_with_ambiguous_selector_fails_with_match_count(self):
        prepared = prepare_step({
            "seq": 2,
            "step_type": "tap",
            "selector": "img",
            "label": "",
            "tag": "img",
        })
        page = FakePage(selector_count=3)

        with self.assertRaises(StepExecutionError) as raised:
            execute_step(page, prepared)

        self.assertEqual(raised.exception.kind, "ambiguous_target")
        self.assertIn("selector 'img' matched 3 elements", str(raised.exception))

    def test_unlabeled_tap_with_missing_selector_reports_missing_target(self):
        prepared = prepare_step({
            "seq": 2,
            "step_type": "tap",
            "selector": "img.missing",
            "label": "",
            "tag": "img",
        })
        page = FakePage(selector_count=0)

        with self.assertRaises(StepExecutionError) as raised:
            execute_step(page, prepared)

        self.assertEqual(raised.exception.kind, "target_not_found")
        self.assertIn("selector 'img.missing' matched no elements", str(raised.exception))

    def test_unlabeled_tap_without_selector_remains_unsupported(self):
        with self.assertRaises(StepExecutionError) as raised:
            prepare_step({"seq": 2, "step_type": "tap", "label": "", "selector": ""})

        self.assertEqual(raised.exception.kind, "unsupported")

    def test_sensitive_input_requires_runtime_fixture(self):
        row = {
            "seq": 5,
            "step_type": "input",
            "label": "Email address",
            "value": "${TEST_EMAIL_ADDRESS}",
            "input_type": "email",
            "role": "textbox",
        }

        with self.assertRaises(StepExecutionError) as raised:
            prepare_step(row)

        self.assertEqual(raised.exception.kind, "fixture_missing")
        self.assertNotIn("person@example", str(raised.exception))

        prepared = prepare_step(row, {"TEST_EMAIL_ADDRESS": "qa@example.test"})
        self.assertEqual(prepared.value, "qa@example.test")

    def test_sensitive_input_without_placeholder_never_reuses_captured_value(self):
        row = {
            "seq": 2,
            "step_type": "input",
            "label": "Password",
            "value": None,
            "input_type": "password",
            "role": "textbox",
        }

        with self.assertRaises(StepExecutionError) as raised:
            prepare_step(row)

        self.assertEqual(raised.exception.kind, "fixture_missing")
        self.assertIn("TEST_PASSWORD", str(raised.exception))

    def test_submit_like_action_is_held_without_opt_in(self):
        prepared = prepare_step({
            "seq": 6,
            "step_type": "tap",
            "label": "Place order",
            "role": "button",
        })

        with self.assertRaises(StepExecutionError) as raised:
            execute_step(FakePage(), prepared)

        self.assertEqual(raised.exception.kind, "held")
        self.assertIn("--allow-submit", str(raised.exception))

    def test_navigation_is_native_and_requires_absolute_http_url(self):
        prepared = prepare_step({
            "seq": 1,
            "step_type": "navigate",
            "page_url": "https://shop.example.test/catalog",
        })

        class NavigationPage:
            def __init__(self):
                self.url = None
                self.navigations = []

            def goto(self, url, **kwargs):
                self.url = url
                self.navigations.append(url)

        page = NavigationPage()
        execute_step(page, prepared)
        self.assertEqual(page.url, prepared.url)
        self.assertEqual(len(page.navigations), 1)
        execute_step(page, prepared)
        self.assertEqual(len(page.navigations), 1)

        verify_navigation = PreparedStep(
            sequence=4,
            step_type="navigate",
            label="Verify navigation",
            url="https://shop.example.test/product",
            navigation_follows_action=True,
        )
        before = len(page.navigations)
        with self.assertRaises(StepExecutionError) as raised:
            execute_step(page, verify_navigation)
        self.assertEqual(raised.exception.kind, "navigation_mismatch")
        self.assertIn("https://shop.example.test/product", str(raised.exception))
        self.assertIn("https://shop.example.test/", str(raised.exception))
        self.assertEqual(len(page.navigations), before)

        with self.assertRaises(StepExecutionError):
            prepare_step({"seq": 2, "step_type": "navigate", "page_url": "javascript:alert(1)"})

        with self.assertRaises(StepExecutionError) as raised:
            prepare_step({
                "seq": 3,
                "step_type": "navigate",
                "page_url": "https://shop.example.test/?token=%5BREDACTED%5D",
            })
        self.assertEqual(raised.exception.kind, "fixture_missing")


if __name__ == "__main__":
    unittest.main()