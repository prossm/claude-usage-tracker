# Third-party notices

## phuryn/claude-usage

`cusage/cctokens.py` adapts the Claude Code JSONL scanning approach from
https://github.com/phuryn/claude-usage (via its copy in
jimdawdy-hub/claude-usage-tracker): incremental scanning by file mtime and
deduplication by `message.id`, where the last streamed record per message wins.

```
MIT License

Copyright (c) 2026 Pawel Huryn

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Acknowledged, no code copied

- **CodeForBreakfast/claude-usage-exporter**: the adaptive polling algorithm
  (`poller.go`) was reimplemented from its behavior. That repo has no license file.
- **jimdawdy-hub/claude-usage-tracker**: using `curl_cffi` Firefox impersonation for
  Cloudflare, and the `extra_usage` cents fields. The README states MIT but the repo
  has no license file.
- **georgewhewell/quota-exporter** (MIT, © 2026 George Whewell): the OAuth usage
  endpoint, its beta header, and the strictly read-only token rule.
