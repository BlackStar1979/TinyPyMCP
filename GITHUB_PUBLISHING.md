# GitHub Publishing Notes

Target repository:

```text
https://github.com/BlackStar1979/TinyPyMCP
```

## Publish Scope

The initial commit should contain source code, tests, documentation, and small
fixtures needed to run the project locally.

The following local/runtime artifacts are intentionally excluded:

- Python bytecode caches and build outputs.
- Local SQLite databases under `data/`.
- Runtime audit logs under `logs/`.
- Search indexes under `workspaces/.index/`.
- Nested Git metadata inside cloned workspaces.
- Local secrets, token files, and environment files.

## Validation

Recommended checks before publishing:

```powershell
python -m compileall src tests main.py
python tests/smoke.py
```

If `python tests/smoke.py` fails because dependencies are missing, install the
project in a local virtual environment first.

## First Push

```powershell
cd C:\Work\TinyPyMCP
git push -u origin main
```

If Git reports dubious ownership, add only this repository as a safe directory:

```powershell
git config --global --add safe.directory C:/Work/TinyPyMCP
```

## License

No open-source license has been selected in this preparation step. Until a
`LICENSE` file is added, publication does not grant reuse rights beyond what
GitHub's terms allow for viewing and forking.
