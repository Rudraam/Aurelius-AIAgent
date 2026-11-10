# The Caddy image for Aurelius: the single, Dependabot-visible pin of the Caddy base image.
# tests/deploy/test_caddy_cimd.py reads its image reference from the FROM line below, so the test and
# the eventual compose service (M1) run the same pinned image.
# Supply-chain rule (CLAUDE.md): pinned by multi-arch INDEX digest, at least 14 days old; the tag
# stays in the reference so Dependabot can propose updates.

# caddy:2.10.2, index built 2026-02-11 (amd64, arm64, and others)
FROM caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb
