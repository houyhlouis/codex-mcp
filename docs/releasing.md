# Source archives

English | [简体中文](releasing.zh-CN.md)

Create the source archive with:

```bash
python scripts/build_source_zip.py
```

For version 0.1.0, the output is `dist/codex-mcp-0.1.0-source.zip` with a SHA-256 checksum. The ZIP contains source, documentation, examples, tests and CI definitions under one `codex-mcp/` directory. It excludes private state, credentials, Git metadata, installed dependencies and caches.

`LICENSE` is intentionally omitted at the owner's request so the repository's existing MIT license file is preserved. Python distribution metadata still declares MIT and references that repository file; build installable Python distributions from a checkout containing it. Running the bridge directly from source does not require a packaging build.

The embedded `SOURCE-MANIFEST.json` records sizes and hashes for the archived files. The archive builder checks its own output for integrity and does not run unit tests or contact a model.
