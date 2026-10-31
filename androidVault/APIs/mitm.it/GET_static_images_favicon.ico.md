---
url: https://mitm.it/static/images/favicon.ico
method: GET
path_template: /static/images/favicon.ico
domain: mitm.it
first_seen: 2026-10-28T13:58:15+00:00
last_seen: 2026-10-28T13:58:15+00:00
capture_count: 1
tags: [api, captured]
---

## Headers
```
{
  "sec-ch-ua-platform": "\"Android\"",
  "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36",
  "sec-ch-ua": "\"Google Chrome\";v=\"155\", \"Chromium\";v=\"155\", \"Not(A:Brand\";v=\"24\"",
  "sec-ch-ua-mobile": "?1",
  "accept": "image/jxl,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
  "sec-fetch-site": "same-origin",
  "sec-fetch-mode": "no-cors",
  "sec-fetch-dest": "image",
  "referer": "https://mitm.it/",
  "accept-encoding": "gzip, deflate, br, zstd",
  "accept-language": "en-GB,en-US;q=0.9,en;q=0.8,vi;q=0.7",
  "priority": "u=1, i"
}
```

## Params
- [[Params/mitm.it_static__binary_body]] _response_

## Concrete captured paths
- `https://mitm.it/static/images/favicon.ico` (captures: 1, first: 2026-10-28T13:58:15+00:00, last: 2026-10-28T13:58:15+00:00)

## Capture provenance
No per-session browser provenance recorded

## Sample Payload
```
(none)
```

## cURL (Postman)
```bash
curl --location --request GET --url 'https://mitm.it/static/images/favicon.ico' --header 'user-agent: Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36' --header 'accept: image/jxl,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8' --header 'referer: https://mitm.it/' --header 'accept-language: en-GB,en-US;q=0.9,en;q=0.8,vi;q=0.7'
```
_Import this cURL command into Postman using **Import → Raw text**. Captured credentials and personal data are not embedded._
Review the method before sending: this request may change server-side data.

## Sample Response
```
{"binary_body": {"content_type": "image/x-icon", "sha256": "48a1de597e0c6871aaab828b6197986ac729c8d8678a93b21d453f799f79061c", "size_bytes": 147}}
```

## Applications / Entry Points
- Android interaction pending correlation

## UI After Call
_Most recent 0 of up to 5 kept, newest first._
_(no screenshot captured yet)_
