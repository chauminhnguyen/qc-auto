---
url: https://mitm.it/
method: GET
path_template: /
domain: mitm.it
first_seen: 2026-10-28T13:58:15+00:00
last_seen: 2026-10-28T13:58:15+00:00
capture_count: 1
tags: [api, captured]
---

## Headers
```
{
  "cache-control": "max-age=0",
  "sec-ch-ua": "\"Google Chrome\";v=\"155\", \"Chromium\";v=\"155\", \"Not(A:Brand\";v=\"24\"",
  "sec-ch-ua-mobile": "?1",
  "sec-ch-ua-platform": "\"Android\"",
  "upgrade-insecure-requests": "1",
  "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36",
  "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
  "sec-fetch-site": "none",
  "sec-fetch-mode": "navigate",
  "sec-fetch-user": "?1",
  "sec-fetch-dest": "document",
  "accept-encoding": "gzip, deflate, br, zstd",
  "accept-language": "en-GB,en-US;q=0.9,en;q=0.8,vi;q=0.7",
  "priority": "u=0, i"
}
```

## Params
- (none)

## Concrete captured paths
- `https://mitm.it/` (captures: 1, first: 2026-10-28T13:58:15+00:00, last: 2026-10-28T13:58:15+00:00)

## Capture provenance
No per-session browser provenance recorded

## Sample Payload
```
(none)
```

## cURL (Postman)
```bash
curl --location --request GET --url 'https://mitm.it/' --header 'cache-control: max-age=0' --header 'upgrade-insecure-requests: 1' --header 'user-agent: Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36' --header 'accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7' --header 'accept-language: en-GB,en-US;q=0.9,en;q=0.8,vi;q=0.7'
```
_Import this cURL command into Postman using **Import → Raw text**. Captured credentials and personal data are not embedded._
Review the method before sending: this request may change server-side data.

## Sample Response
```
%3C%21doctype+html%3E%0A%3Chtml+lang=%22en%22%3E%0A%3Chead%3E%0A++++%3Cmeta+charset%3D%22utf-8%22%3E%0A++++%3Cmeta+name%3D%22viewport%22+content%3D%22width%3Ddevice-width%2C+initial-scale%3D1%2C+shrink-to-fit%3Dno%22%3E%0A%0A++++%3Ctitle%3Emitmproxy%3C%2Ftitle%3E%0A++++%3Clink+href%3D%22static%2Fbootstrap.min.css%22+rel%3D%22stylesheet%22%3E%0A++++%3Clink+href%3D%22static%2Fmitmproxy.css%22+rel%3D%22stylesheet%22%3E%0A++++%3Clink+rel%3D%22icon%22+href%3D%22static%2Fimages%2Ffavicon.ico%22+type%3D%22image%2Fx-icon%22%2F%3E%0A%3C%2Fhead%3E%0A%0A%3Cbody%3E%0A%3Cnav+class%3D%22navbar+navbar-dark+bg-dark+mb-3%22%3E%0A++++%3Cdiv+class%3D%22container%22%3E%0A++++++++%3Cdiv+class%3D%22navbar-header%22%3E%0A++++++++++++%3Cdiv+class%3D%22navbar-brand%22%3E%0A++++++++++++++++%3Cimg+src%3D%22static%2Fimages%2Fmitmproxy-long.png%22+height%3D%2230%22+alt%3D%22mitmproxy+logo%22+class%3D%22align-top%22%2F%3E%0A++++++++++++%3C%2Fdiv%3E%0A++++++++%3C%2Fdiv%3E%0A++++%3C%2Fdiv%3E%0A%3C%2Fnav%3E%0A%3Cdiv+class%3D%22container%22%3E%0A++++%0A%3Cdiv+class%3D%22row+justify-content-md-center%22%3E%0A++++%3Cdiv+class%3D%22col-md-9%22%3E%0A%3C%21--suppress+HtmlUnknownTag+--%3E%0A%3Ch3+class%3D%22my-4%22%3EInstall+mitmproxy%27s+Certificate+Authority%3C%2Fh3%3E%0A%0A%0A%0A%3Cul+class%3D%22list-unstyled%22%3E%0A++++%3Cli+class%3D%22media%22%3E%0A++++%3Csvg+aria-hidden%3D%22true%22+focusable%3D%22false%22+data-prefix%3D%22fab%22+data-icon%3D%22windows%22+class%3D%22svg-inline--fa+fa-windows+fa-w-14%22+role%3D%22img%22+xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22+viewBox%3D%220+0+448+512%22%3E%3Cpath+fill%3D%22currentColor%22+d%3D%22M0+93.7l183.6-25.3v177.4H0V93.7zm0+324.6l183.6+25.3V268.4H0v149.9zm203.8+28L448+480V268.4H203.8v177.9zm0-380.6v180.1H448V32L203.8+65.7z%22%3E%3C%2Fpath%3E%3C%2Fsvg%3E%0A++++%3Cdiv+class%3D%22media-body%22%3E%0A++++++++%3Ch5+class%3D%22mt-0%22%3EWindows%3C%2Fh5%3E%0A++++++++%3Ca+class%3D%22btn+btn-sm+btn-success%22+href%3D%22%2Fcert%2Fp12%22+target%3D%22_blank%22%3E%F0%9F%94%8F+Get+mitmproxy-ca-cert.p12%3C%2Fa%3E%0A++++++++%3Ca+class%3D%22btn+btn-sm+btn-info+show-instructions%22+href%3D%22%23Windows%22+id%3D%22Windows%22%3E%F0%9F%93%96%0A++++++++++++Show+Instructions%3C%2Fa%3E%0A++++++++%3Ca+class%3D%22btn+btn-sm+btn-info+hide-instructions%22+href%3D%22%23%2F%22%3E%F0%9F%93%96+Hide+Instructions%3C%2Fa%3E%0A++++++++%3Cdiv+class%3D%22instructions%22%3E%0A++++%3Ch5%3EManual+Installation%3C%2Fh5%3E%0A++++%3Col%3E%0A++++++++%3Cli%3EDouble-click+the+P12+file+to+start+the+import+wizard.%3C%2Fli%3E%0A++++++++%3Cli%3ESelect+a+certificate+store+location.+This+determines+who+will+trust+the+certificate+&ndash%3B+only+the+current%0A++++++++++++Windows+user+or+everyone+on+the+machine.+Click+%3Csamp%3ENext%3C%2Fsamp%3E.%0A++++++++%3C%2Fli%3E%0A++++++++%3Cli%3EClick+%3Csamp%3ENext%3C%2Fsamp%3E+again.%3C%2Fli%3E%0A++++++++%3Cli%3ELeave+%3Csamp%3EPassword%3C%2Fsamp%3E+blank+and+click+%3Csamp%3ENext%3C%2Fsamp%3E.%3C%2Fli%3E%0A++++++++%3Cli%3E%3Cspan+class=%5BREDACTED%5D&+Device+Management%3C%2Fsamp%3E%29.%3C%2Fli%3E%0A++++++++%3Cli%3E%3Cspan+class=%22text-danger%22%3E%3Cstrong%3EImportant%3A%3C%2Fstrong%3E+Go+to+%3Csamp%3ESettings+-%3E+General+-%3E+About+-%3E+Certificate+Trust+Settings%3C%2Fsamp%3E.%0A++++++++++++Toggle+%3Csamp%3Emitmproxy%3C%2Fsamp%3E+to+%3Csamp%3EON%3C%2Fsamp%3E.%3C%2Fspan%3E%3C%2Fli%3E%0A++++%3C%2Fol%3E%0A++++%3C%2Fdiv%3E%0A++++%3C%2Fdiv%3E%0A%3C%2Fli%3E%0A++++%3Cli+class%3D%22media%22%3E%0A++++%3Csvg+aria-hidden%3D%22true%22+focusable%3D%22false%22+data-prefix%3D%22fab%22+data-icon%3D%22android%22+class%3D%22svg-inline--fa+fa-android+fa-w-18%22+role%3D%22img%22+xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22+viewBox%3D%220+0+576+512%22%3E%3Cpath+fill%3D%22currentColor%22+d%3D%22M420.55%2C301.93a24%2C24%2C0%2C1%2C1%2C24-24%2C24%2C24%2C0%2C0%2C1-24%2C24m-265.1%2C0a24%2C24%2C0%2C1%2C1%2C24-24%2C24%2C24%2C0%2C0%2C1-24%2C24m273.7-144.48%2C47.94-83a10%2C10%2C0%2C1%2C0-17.27-10h0l-48.54%2C84.07a301.25%2C301.25%2C0%2C0%2C0-246.56%2C0L116.18%2C64.45a10%2C10%2C0%2C1%2C0-17.27%2C10h0l47.94%2C83C64.53%2C202.22%2C8.24%2C285.55%2C0%2C384H576c-8.24-98.45-64.54-181.78-146.85-226.55%22%3E%3C%2Fpath%3E%3C%2Fsvg%3E%0A++++%3Cdiv+class%3D%22media-body%22%3E%0A++++++++%3Ch5+class%3D%22mt-0%22%3EAndroid%3C%2Fh5%3E%0A++++++++%3Ca+class%3D%22btn+btn-sm+btn-success%22+href%3D%22%2Fcert%2Fcer%22+target%3D%22_blank%22%3E%F0%9F%94%8F+Get+mitmproxy-ca-cert.cer%3C%2Fa%3E%0A++++++++%3Ca+class%3D%22btn+btn-sm+btn-info+show-instructions%22+href%3D%22%23Android%22+id%3D%22Android%22%3E%F0%9F%93%96%0A++++++++++++Show+Instructions%3C%2Fa%3E%0A++++++++%3Ca+class%3D%22btn+btn-sm+btn-info+hide-instructions%22+href%3D%22%23%2F%22%3E%F0%9F%93%96+Hide+Instructions%3C%2Fa%3E%0A++++++++%3Cdiv+class%3D%22instructions%22%3E%0A++++%3Ch5%3EAndroid+10+%3C%2Fh5%3E%0A++++%3Col+class%3D%22mb-2%22%3E%0A++++++++%3Cli%3EOpen+the+downloaded+CER+file.%3C%2Fli%3E%0A++++++++%3Cli%3EEnter+%3Csamp%3Emitmproxy%3C%2Fsamp%3E+%28or+anything+else%29+as+the+certificat
```

## Applications / Entry Points
- Android interaction pending correlation

## UI After Call
_Most recent 0 of up to 5 kept, newest first._
_(no screenshot captured yet)_
