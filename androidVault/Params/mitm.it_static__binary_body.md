---
tags: [param]
resource: static
domain: mitm.it
canonical_name: binary_body
wire_names: ["binary_body"]
seen_in_apis: 2
seen_as: response
---

## Definition
_Not yet analyzed — run `enrich.py` to generate a definition for this param._

_Note: this note only covers "binary_body" as used under the **static** resource.
The same name may exist as a completely separate, independently-defined param under other resources._

## Used by
- [[GET_static_images_favicon.ico]] _response_
- [[GET_static_images_mitmproxy-long.png]] _response_

## Example values seen (request)
- (none)

## Example values seen (response)
- `{"content_type": "image/png", "sha256": "e94e704fc11f39a7996561a3a8a305c91e06333397a760cebae79bf92bab2712", "size_bytes": 146}`
- `{"content_type": "image/x-icon", "sha256": "48a1de597e0c6871aaab828b6197986ac729c8d8678a93b21d453f799f79061c", "size_bytes": 147}`
