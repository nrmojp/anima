## Summary

## Boundary and dependency impact

- [ ] Core does not import Capabilities, Plugins, Adapters, or external SDKs.
- [ ] Capability contracts do not import concrete Plugins or Adapters.
- [ ] Plugin code imports only Core/Capability contracts, the standard library, and
      code contained in its own directory.
- [ ] New services are sandbox-bound and narrowly scoped.

## Verification

- [ ] Unit tests pass.
- [ ] Coverage was reviewed; new SDK-free classes reach 100% where practical.
- [ ] Architecture and Dashboard tests pass.
- [ ] `python scripts/check_public_tree.py` passes.
- [ ] `git diff --check` passes.
- [ ] No credentials, IDs, private persona data, state, logs, or media were added.
