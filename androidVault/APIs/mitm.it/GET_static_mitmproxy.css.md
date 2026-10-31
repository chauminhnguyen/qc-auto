---
url: https://mitm.it/static/mitmproxy.css
method: GET
path_template: /static/mitmproxy.css
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
  "accept": "text/css,*/*;q=0.1",
  "sec-fetch-site": "same-origin",
  "sec-fetch-mode": "no-cors",
  "sec-fetch-dest": "style",
  "referer": "https://mitm.it/",
  "accept-encoding": "gzip, deflate, br, zstd",
  "accept-language": "en-GB,en-US;q=0.9,en;q=0.8,vi;q=0.7",
  "priority": "u=0"
}
```

## Params
- (none)

## Concrete captured paths
- `https://mitm.it/static/mitmproxy.css` (captures: 1, first: 2026-10-28T13:58:15+00:00, last: 2026-10-28T13:58:15+00:00)

## Capture provenance
No per-session browser provenance recorded

## Sample Payload
```
(none)
```

## cURL (Postman)
```bash
curl --location --request GET --url 'https://mitm.it/static/mitmproxy.css' --header 'user-agent: Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36' --header 'accept: text/css,*/*;q=0.1' --header 'referer: https://mitm.it/' --header 'accept-language: en-GB,en-US;q=0.9,en;q=0.8,vi;q=0.7'
```
_Import this cURL command into Postman using **Import → Raw text**. Captured credentials and personal data are not embedded._
Review the method before sending: this request may change server-side data.

## Sample Response
```
.media {
    min-height: 110px;
}
.media svg {
    width: 64px;
    margin-right: 1rem !important;
}

.instructions {
    padding-top: 1rem;
    padding-bottom: 1rem;
}

/* CSS-only collapsible */
.show-instructions:target, .hide-instructions, .instructions {
    display: none;
}
.show-instructions:target ~ .hide-instructions {
    display: inline-block;
}
.show-instructions:target ~ .instructions {
    display: inherit;
}

.fa-apple {
    color: #666;
}

.fa-windows {
    color: #0078D7;
}

.fa-firefox-browser {
    color: #E25821;
}

.fa-android {
    margin-top: 10px;
    color: #3DDC84;
}

.fa-certificate {
    color: #FFBB00;
}
```

## Applications / Entry Points
- Android interaction pending correlation

## UI After Call
_Most recent 0 of up to 5 kept, newest first._
_(no screenshot captured yet)_
