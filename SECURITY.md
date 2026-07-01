# Security Policy

## Supported Versions

The `main` branch is the only actively supported line. Older tags are
not patched. The most recent release is the supported release until the
next release ships.

## Reporting a Vulnerability

**Please do not file a public issue for security vulnerabilities.**

Open a GitHub Security Advisory:

`https://github.com/gregyoung14/openmarket/security/advisories/new`

Include:

- A clear description of the vulnerability and its impact
- Reproduction steps or a minimal proof of concept
- The affected versions / commits
- Any known mitigations

You should receive an acknowledgement within 72 hours. The maintainer
will work with you on a coordinated disclosure timeline; please allow a
reasonable window (typically up to 90 days) before any public
disclosure.

## Scope

In scope for this repository:

- Code execution from untrusted input via the Rust collectors, recorder,
  backtester, or execution engine
- SQL injection or path traversal in `scripts/datasets/`,
  `scripts/hf/`, or `datasets/download.py`
- Unsafe Rust that is reachable from a public API
- Dependency vulnerabilities with a known exploit

Out of scope:

- Issues in third-party SDKs (`polymarket-client-sdk`, `ethers`, etc.) —
  report upstream
- Bugs that require access to a live Polymarket or Binance account
- Findings against deployed infrastructure the maintainer does not own

## Historical Note on Leaked Credentials

Earlier private development snapshots of this project (predating the
first public release tag `v0.1.0`) contained hardcoded production
credentials — a Polygon wallet private key, Polymarket L2 API
credentials, and a Bunny CDN access key — committed to a private git
repository. Those credentials were rotated before this open-source
repository was created. The public repository and Hugging Face
datasets contain no live credentials.

If you discover a credential leak in this repository, please follow the
disclosure process above and do not attempt to use or test the leaked
material.

## Secrets and CI

The CI workflows in `.github/workflows/` reference a `HF_TOKEN` GitHub
secret used by the dataset publish and validate jobs. That secret is a
Hugging Face write token scoped to the `gregyoung14` org and is
rotated on maintainer departure or suspected exposure. The release
workflow reads no other secrets.