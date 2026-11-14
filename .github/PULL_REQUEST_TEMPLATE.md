## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How I tested it

<!-- List the commands you actually ran and on which system. Say plainly what you could not test. -->

- [ ] `python3 -m unittest discover -v` passes
- [ ] `python3 -m compileall -q codex_local_mcp tests scripts server.py` passes
- Operating system and Python version:
- Not tested:

## Checklist

- [ ] A test covers the change, if it touches recovery, retries, races, approvals or permission checks
- [ ] No new way to run raw Codex requests, an unrestricted shell, or to approve without a person
- [ ] README and the affected page in `docs/` are updated (and the Chinese page, if one exists)
- [ ] `CHANGELOG.md` has an entry for a change users can see
- [ ] No credentials, personal paths or runtime data in the diff
