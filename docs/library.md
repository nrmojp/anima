# Using Anima as a library

Anima is both a reusable Python runtime and an optional standalone bot host.
The distribution contains only the `anima` package, dashboard assets, and typing
marker. Persona configuration, credentials, state, and model/media files belong
to the consuming application, not to the wheel.

## Dependency and Python version

Use Python 3.14 or later. Until a release is published, install from an explicitly
reviewed Git commit, not a moving branch or an unrelated package with the same name:

```sh
python -m pip install 'anima[bot] @ git+https://github.com/nrmojp/anima.git@<full-commit-sha>'
```

For joint local development, use an editable checkout:

```sh
python -m pip install -e '../anima[bot]'
```

Release wheels can also be installed by an explicit GitHub Releases asset URL.
Pin a version and artifact SHA-256 in deployment dependency locks. This project
does not require or assume a Python registry at GitHub Packages. Publishing a
release or changing repository visibility is a separate maintainer action.

## External application plugins

Keep application code outside the `anima` namespace:

```text
my_bot/
  __init__.py
  plugins/
    __init__.py
    greeting/
      __init__.py
      plugin.py
```

`plugin.py` exports `PLUGIN`, a definition implementing the existing
`PluginDefinition` protocol. See [plugin development](plugin-development.md).
Install this application package alongside Anima, then configure:

```sh
ANIMA_PLUGIN_NAMESPACE=my_bot.plugins anima run
```

The namespace replaces the reference `anima.plugins` catalog; it does not merge
or override plugins with matching names. All host discovery paths (settings,
commands, dashboard, lifecycle, and CLI hooks) use that same namespace. An
explicit `PluginLoader("my_bot.plugins")` takes precedence over the environment.
Invalid namespace syntax fails rather than falling back to reference plugins.
Only load trusted Python packages: this is not a security isolation mechanism.
Do not allow a model or ordinary Discord user to change the namespace.

The configured package must be installed before startup, and changing discovery
requires a process restart. `ANIMA_PLUGINS` still selects enabled plugin IDs
within the discovered catalog. Plugin dependencies are declared and installed
by the consuming application's package, not downloaded by PluginLoader.

## API boundaries

Use `anima.core` for runtime contracts and `anima.capabilities` for plugin
contracts. Use `anima.bootstrap` only for the optional assembled host; adapters
provide concrete integrations. Plugins should consume sandbox-scoped services
rather than reconstruct global stores or import another application's modules.
Version 0.x is evolving: pin dependencies and run application regression tests
before upgrading. No compatibility promise is made for private helpers.

## Build and verify the artifact

```sh
python -m pip install build
python -m build --wheel
python scripts/check_wheel.py dist/anima-0.1.0-py3-none-any.whl
```

Install that wheel in a fresh Python 3.14 environment outside this source tree.
Verify core imports without bot extras, then install bot extras and test plugin
discovery and the host. A source-tree test alone cannot detect missing package
files or dashboard assets. The CI packaging job performs the basic isolated
wheel checks; application integration tests remain the consumer's responsibility.

Anima can be published independently of private applications. Wheel checks do
not replace a review of repository contents and history before making a private
repository public.
