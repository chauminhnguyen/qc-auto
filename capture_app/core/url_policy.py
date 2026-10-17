"""Shared URL filtering policy for recording and crawling."""

IGNORE_DOMAIN_SUBSTRINGS = [
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.com/tr", "connect.facebook.net", "sentry.io", "fullstory.com",
    "hotjar.com", "segment.io", "mixpanel.com", "bugsnag.com",
    "cloudflareinsights.com", "newrelic.com", "amplitude.com",
    "cookielaw.org", "onetrust.com", "mouseflow.com", "optimizely.com",
    "google.com/rmkt", "analytics.google.com", "googleadservices.com",
    "bzrcdn.openai.com",
]


def is_ignored(url: str) -> bool:
    return any(domain in url for domain in IGNORE_DOMAIN_SUBSTRINGS)