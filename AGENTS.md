# Development guidelines

- Keep `anima.core` independent from adapters and plugins.
- Keep `anima.capabilities` independent from concrete plugins and external SDKs.
- Add or update unit tests for every behavior change.
- Run `python -m unittest discover -s tests -v`, coverage, and `git diff --check`
  before committing.
- Aim for 100% line coverage for classes without external dependencies.
- Never commit secrets, runtime state, generated media, or persona-specific data.
