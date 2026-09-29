# Team standards (edit me — committed to git, read on every review)

## General
- Every public function that does I/O must handle failures explicitly; no bare `except:`.
- No secrets, tokens or credentials in code or logs.
- New behaviour needs a test. Bug fixes need a regression test.

## Architecture
- Layering: handlers -> services -> repositories. Never skip a layer.
- Database access only inside repositories; always use parameterized queries.

## Python
- Type hints on all public functions.
- Prefer `pathlib` over `os.path`.
