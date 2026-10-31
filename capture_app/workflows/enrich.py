"""
enrich.py
---------
Run this AFTER capturing traffic with capture.py. Pick one or more already-
captured APIs at once, and this:

  1. Asks an LLM (via OpenRouter) to invent payload variants for the
     selected APIs, batching as many into a single call as their payload
     sizes comfortably allow (not one call per API -- keeps token/call
     usage down). Each call's token budget is sized off the actual payload
     JSON, not a flat guess, so a heavy nested payload doesn't starve a
     batch of lighter ones. If a call still comes back truncated or
     unparsable (a free model looping, running out of budget, etc.), the
     batch is split in half and retried rather than losing every API in it.
  2. Executes GET/HEAD/OPTIONS variants automatically. For POST/PUT/PATCH/
     DELETE, shows you the variants per API and REQUIRES confirmation
     before sending anything -- these can change real state on a real
     service.
  3. Feeds the LLM every variant -> response pair from every API it
     actually executed, in a SECOND single call, and asks it to write a
     short definition of what each param does.
  4. Writes those definitions into Params/<name>.md in the vault.

Requires:
    OPENROUTER_API_KEY   (free key: https://openrouter.ai/keys)
    OPENROUTER_MODEL      optional, see openrouter.py for details
    ENRICH_MAX_TOKENS_CAP  optional, ceiling per LLM call (default 6000) --
                           raise it if your model supports a bigger context
                           and you're seeing a lot of batch-splitting

Run:
    python enrich.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from capture_app.env import load_project_env
import httpx

from capture_app.integrations.openrouter import OpenRouterError, chat_json
from capture_app.storage.vault import VaultWriter, redact_http_response

load_project_env()

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
VARIANT_COUNT = 3
REQUEST_DELAY_SECONDS = 1.0  # be polite to the target site between variant requests
DROP_HEADERS = {"content-length", "host"}  # let httpx recompute these itself

# Ceiling per LLM call, overridable since it depends on what your chosen
# OpenRouter model actually supports -- raising it blindly can just move the
# failure from "truncated JSON" to "model's own context limit exceeded".
MAX_TOKENS_CAP = int(os.environ.get("ENRICH_MAX_TOKENS_CAP", "6000"))

# Rough JSON-output cost model used to size each LLM call instead of a flat
# per-API guess -- a payload with nested objects/arrays (e.g. a pet with
# category/tags/photoUrls) needs a lot more output budget per variant than a
# handful of flat scalar fields does.
CHARS_PER_TOKEN_ESTIMATE = 3.5
PER_API_OVERHEAD_TOKENS = 80   # JSON scaffolding: braces, the api-id key, array wrapper
BUDGET_SAFETY_MARGIN = 1.5     # headroom so a slightly verbose model still finishes cleanly
MAX_SPLIT_DEPTH = 6            # more than enough to split any realistic selection down to 1 API each


def choose_apis(vault: VaultWriter) -> list:
    """Returns a list of api dicts. Supports comma-separated indices or 'all'."""
    apis = vault.list_apis_with_params()
    if not apis:
        print("No captured APIs with params found yet. Run capture.py first.")
        return []

    print("\nCaptured APIs with params:\n")
    for i, api in enumerate(apis, 1):
        print(f"  [{i}] {api['method']} {api['full_url']}  "
              f"({api['param_count']} params, {api['capture_count']} captures)")

    choice = input(
        "\nPick numbers to enrich together, comma-separated (e.g. 1,3,5), "
        "'all', or 'q' to quit: "
    ).strip()
    if choice.lower() == "q" or not choice:
        return []
    if choice.lower() == "all":
        return apis

    selected = []
    for part in choice.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            selected.append(apis[int(part) - 1])
        except (ValueError, IndexError):
            print(f"  skipping invalid choice: '{part}'")
    return selected


# ----------------------------------------------------------------------
# Step 1: one batched call to generate variants for every selected API
# ----------------------------------------------------------------------
def build_batch_variant_prompt(bundle: list) -> tuple:
    """bundle: list of {'api': api_dict, 'params': [param_dict, ...]}"""
    system = (
        "You generate test payload variants for MULTIPLE HTTP API requests "
        "in a single response. Respond with ONLY a JSON object, no prose, "
        "no markdown fences. Keys must be the exact API id strings given "
        f"below. Each value is an array of exactly {VARIANT_COUNT} variant "
        "objects, each mapping every param name for that API to a new "
        "value. Vary values meaningfully (different types, boundary "
        "values, realistic alternates) per API so effects on the response "
        "can be compared. Keep every variant compact and well-formed: at "
        "most 2 items in any array-valued field (e.g. tags, photoUrls), "
        "and never repeat a key or an object within the same variant or "
        "across variants -- each variant is a single, complete, minimal "
        "JSON object with no duplicated substructures."
    )
    blocks = []
    for item in bundle:
        api, params = item["api"], item["params"]
        param_desc = "\n".join(
            f"  - {p['name']}: example value = {p['examples'][0] if p['examples'] else ''}"
            for p in params
        )
        blocks.append(
            f'API id: "{api["ref_stem"]}"\n'
            f'Method: {api["method"]}\nURL: {api["full_url"]}\n'
            f'Current payload: {api["payload_json"]}\nParams:\n{param_desc}'
        )
    user = "\n\n".join(blocks) + (
        '\n\nReturn JSON like: {"api_id_1": [{"p":"v"}, ...], "api_id_2": [...]}'
    )
    return system, user


def estimate_output_tokens(bundle: list) -> int:
    """Sizes the expected output off each API's actual current payload
    rather than a flat per-API guess, so a heavy nested payload gets
    proportionally more budget than a handful of flat scalar fields."""
    total = 0
    for item in bundle:
        payload_len = len(item["api"].get("payload_json") or "{}")
        per_variant = payload_len / CHARS_PER_TOKEN_ESTIMATE + PER_API_OVERHEAD_TOKENS
        total += int(per_variant * VARIANT_COUNT)
    return total


def _is_json_parse_failure(err: OpenRouterError) -> bool:
    # This is the shape chat_json() raises when the model's own output
    # didn't parse -- truncation (ran out of tokens) or a formatting/
    # repetition glitch. Distinguishing it from config/network errors (bad
    # key, bad status, etc.) matters because only THIS kind is worth
    # retrying with a bigger budget or a smaller batch; the others would
    # just fail identically again no matter how the batch is split.
    return str(err).startswith("Model did not return valid JSON")


def generate_variants_for_bundle(bundle: list, depth: int = 0) -> dict:
    """Returns a variant_map ({ref_stem: [variant, ...]}) covering as much
    of `bundle` as the LLM can actually produce. If a call comes back
    truncated or unparsable, splits the batch in two and retries each half
    (with its own, correspondingly smaller token budget) instead of losing
    every API in the batch to one oversized or looping payload."""
    if not bundle:
        return {}

    max_tokens = min(MAX_TOKENS_CAP, max(800, int(estimate_output_tokens(bundle) * BUDGET_SAFETY_MARGIN)))
    system, user = build_batch_variant_prompt(bundle)

    try:
        result = chat_json(system, user, max_tokens=max_tokens, temperature=0.3)
        if not isinstance(result, dict):
            raise OpenRouterError(f"Expected a JSON object keyed by API id, got: {result}")
        return result
    except OpenRouterError as e:
        if not _is_json_parse_failure(e):
            print(f"LLM error: {e}")
            if depth == 0:
                raise  # config/network-level problem -- let main() stop the whole run
            return {}  # mid-split: don't let one bad chunk take its siblings down with it

        ref_stems = [item["api"]["ref_stem"] for item in bundle]
        if len(bundle) == 1 or depth >= MAX_SPLIT_DEPTH:
            print(f"  [enrich] gave up on {ref_stems} after budget retries ({max_tokens} tokens): {e}")
            return {}

        print(f"  [enrich] {len(bundle)} API(s) at ~{max_tokens} tokens came back cut off/unparsable "
              f"-- splitting into smaller batches and retrying...")
        mid = len(bundle) // 2
        left = generate_variants_for_bundle(bundle[:mid], depth + 1)
        right = generate_variants_for_bundle(bundle[mid:], depth + 1)
        return {**left, **right}


def clean_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items()
            if k.lower() not in DROP_HEADERS and not k.startswith(":")}


def apply_variant_to_request(api: dict, variant: dict):
    """Returns (url, method, headers, json_body_or_None, form_body_or_None)."""
    headers = clean_headers(json.loads(api["headers_json"] or "{}"))
    content_type = headers.get("Content-Type", headers.get("content-type", ""))
    url = api["full_url"]
    method = api["method"]

    # Bodyless methods carry generated params in the URL. Previously these
    # variants were silently discarded, causing every GET probe to repeat the
    # captured URL unchanged.
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        parts = urlsplit(url)
        variant_names = {str(key) for key in variant}
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                 if key not in variant_names]
        query.extend((str(key), value) for key, values in variant.items()
                     for value in (values if isinstance(values, list) else [values]))
        url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                          urlencode(query, doseq=True), parts.fragment))
        return url, method, headers, None, None

    if "json" in content_type:
        return url, method, headers, variant, None
    elif "x-www-form-urlencoded" in content_type:
        return url, method, headers, None, variant
    else:
        # No recognized body content-type -- send as query params instead.
        return url, method, headers, None, None


def run_variants(api: dict, variants: list) -> list:
    results = []
    for i, variant in enumerate(variants, 1):
        url, method, headers, json_body, form_body = apply_variant_to_request(api, variant)
        print(f"    sending variant {i}/{len(variants)}: {variant}")
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                resp = client.request(method, url, headers=headers, json=json_body, data=form_body)
            safe_headers, safe_body = redact_http_response(dict(resp.headers), resp.text)
            results.append({
                "variant": variant,
                "status": resp.status_code,
                "response_headers": safe_headers,
                "response_body": safe_body[:5000],
            })
        except httpx.RequestError as e:
            results.append({
                "variant": variant, "status": None,
                "response_headers": {}, "response_body": f"<request failed: {e}>",
            })
        time.sleep(REQUEST_DELAY_SECONDS)
    return results


# ----------------------------------------------------------------------
# Step 2: one batched call to define every param across every API that
# actually got executed
# ----------------------------------------------------------------------
def build_batch_definition_prompt(enriched: list) -> tuple:
    """enriched: list of (api_dict, params_list, results_list)"""
    system = (
        "You analyze API test results to explain what each request "
        "parameter does. Respond with ONLY a JSON object mapping each key "
        "given below -- EXACTLY as given, in the form 'resource::param' -- "
        "to a concise 1-2 sentence definition, based on how responses "
        "changed across variants. Params under the SAME 'resource::' prefix "
        "came from endpoints that share a resource and may reasonably share "
        "one definition. Params under DIFFERENT resource prefixes may have "
        "the exact same param name (e.g. 'pet::id' vs 'user::id') but come "
        "from unrelated resources -- treat those as unrelated params that "
        "happen to share a name, and never blend their meanings together. "
        "No prose, no markdown fences."
    )
    blocks = []
    for api, params, results in enriched:
        lines = [
            f"  payload={json.dumps(r['variant'])} -> status={r['status']} "
            f"response={r['response_body'][:400]}"
            for r in results
        ]
        keys = [f'{api["resource_key"]}::{p["name"]}' for p in params]
        blocks.append(
            f'API: {api["method"]} {api["full_url"]} (resource: {api["resource_key"]})\n'
            f'Keys to define: {keys}\n' + "\n".join(lines)
        )
    user = "\n\n".join(blocks) + (
        '\n\nReturn JSON like: {"resource_a::param1": "definition text", '
        '"resource_b::param1": "a different definition, unrelated to the one above"}'
    )
    return system, user


def main():
    vault_path = Path(input("Obsidian vault folder path [./ObsidianVault]: ").strip() or "./ObsidianVault")
    vault = VaultWriter(vault_path)

    apis = choose_apis(vault)
    if not apis:
        return

    bundle = []
    for api in apis:
        params = vault.get_params_for_api(api["ref_stem"], role="request")
        if params:
            bundle.append({"api": api, "params": params})
        else:
            print(f"  skipping {api['method']} {api['full_url']} -- no linked params.")
    if not bundle:
        print("None of the selected APIs have params to enrich.")
        return

    print(f"\nAsking the LLM to generate variants for {len(bundle)} API(s)...")
    try:
        variant_map = generate_variants_for_bundle(bundle)
    except OpenRouterError as e:
        print(f"LLM error: {e}")
        return
    if not variant_map:
        print("No variants could be generated for any selected API -- nothing to enrich.")
        return

    enriched = []  # (api, params, results) for APIs that actually got executed
    for item in bundle:
        api, params = item["api"], item["params"]
        variants = variant_map.get(api["ref_stem"])
        if not variants:
            print(f"  no variants returned for {api['ref_stem']}, skipping.")
            continue

        if api["method"] not in SAFE_METHODS:
            print(f"\n\u26a0\ufe0f  {api['method']} {api['full_url']} can change real state on the target site.")
            confirm = input(
                f"Send {len(variants)} LLM-generated variant requests to this LIVE endpoint? [y/N]: "
            ).strip().lower()
            if confirm != "y":
                print("  Skipped.")
                continue

        print(f"  executing {len(variants)} variants against {api['full_url']} ...")
        results = run_variants(api, variants)
        for r in results:
            vault.save_variant_run(api["id"], r["variant"], r["status"], r["response_headers"], r["response_body"])
        enriched.append((api, params, results))

    if not enriched:
        print("Nothing was executed -- no definitions to generate.")
        return

    total_params = sum(len(p) for _, p, _ in enriched)
    print(f"\nAsking the LLM to define params across {len(enriched)} API(s) in one call...")
    try:
        system, user = build_batch_definition_prompt(enriched)
        max_tokens = min(MAX_TOKENS_CAP, 200 * max(total_params, 1))
        definitions = chat_json(system, user, max_tokens=max_tokens)
        if not isinstance(definitions, dict):
            raise OpenRouterError(f"Expected a JSON object of definitions, got: {definitions}")
    except OpenRouterError as e:
        print(f"LLM error while generating definitions: {e}")
        return

    for key, definition in definitions.items():
        if "::" not in key:
            print(f"  skipping malformed key from LLM (expected 'resource::name'): {key!r}")
            continue
        resource_key, name = key.split("::", 1)
        vault.set_param_definition(name, resource_key, str(definition))
        print(f"  [{resource_key}] {name}: {definition}")

    print(f"\nDone. Updated Params/*.md for: {', '.join(definitions.keys())}")


if __name__ == "__main__":
    main()
