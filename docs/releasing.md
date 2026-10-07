# Release and publication checklist

1. Confirm every milestone item intended for the release is complete.
2. Run unit tests, coverage, architecture tests, and `git diff --check`.
3. Run `PUBLIC_SCAN_DENY_TERMS='<private names>' python scripts/check_public_tree.py`.
4. Inspect `git log --all --stat` and `git rev-list --objects --all` for private assets.
5. Build the Docker image from a fresh checkout.
6. Start the text bot with dedicated test credentials and verify Discord reply and
   Dashboard health. Never use production credentials in CI.
7. Confirm dependency licenses and sample-content provenance.
8. Update version and release notes, then create a signed tag `vX.Y.Z`.
9. Publish the GitHub release. Package-index publication is a separate explicit step.

Releases contain source archives only. Do not attach or publish a prebuilt Docker image.
Changing this distribution model requires a separate review of bundled dependencies,
license texts, notices, base-image packages, and other redistributed artifacts.

Keep the public repository history self-contained. Never merge a private application
repository or its historical branches into this repository.
