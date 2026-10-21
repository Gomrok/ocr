# Recognition models

`Arabic.traineddata` is the unmodified Arabic-script model from
[tesseract-ocr/tessdata_best](https://github.com/tesseract-ocr/tessdata_best/blob/e12c65a915945e4c28e237a9b52bc4a8f39a0cec/script/Arabic.traineddata),
revision `e12c65a915945e4c28e237a9b52bc4a8f39a0cec`.

- SHA-256: `a80454f72df35c6f52550551dd69b8521ea6a62d834396d1c7dce4c35493902e`
- License: Apache-2.0, included in `LICENSE.tessdata_best`.
- Use: bounded verification of ambiguous numeral glyphs, not replacement of
  the Persian prose model. No customer data was used to train this asset.

The existing `fas` and `eng` files are byte-identical to official
`tesseract-ocr/tessdata_best` revision `e12c65a915945e4c28e237a9b52bc4a8f39a0cec`.
The historical download origin is unknown; identity was checked against the
official Git tree on 2026-10-21, not inferred from a filename or hash alone.

| File | Upstream Git blob | SHA-256 |
| --- | --- | --- |
| fas.traineddata | 326a26e8522dce12596fc0de314710c5cc7055d0 | 99e420969b5ddd2cb135b416316a7ed417c59c4faf9e0d28941348f6448114df |
| eng.traineddata | 176dc3220de7db34d3b3aecbfa42043a6038348b | 8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba |

[Pinned official tree](https://api.github.com/repos/tesseract-ocr/tessdata_best/git/trees/e12c65a915945e4c28e237a9b52bc4a8f39a0cec).
Candidate profiles enforce these hashes and OEM 1, with `fas`, `fas+eng`, or `eng`.
The baseline already uses these same best files; there is no measured best-vs-fast
improvement to claim. No model is downloaded during recognition. OSD is not
installed in this project; page rotation requires explicit operator input.
