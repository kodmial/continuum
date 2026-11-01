# Security Policy

## Supported versions and refs

Continuum is distributed as reusable GitHub Actions workflows and caller
templates, consumed by Git ref rather than as a versioned package. Security
fixes are made on `main`. There is no LTS branch, no guaranteed backport to an
older commit or tag, and no support window for a pinned ref. Consumers that pin
a tag or commit should update the pin and re-run `install.sh` for every set they
use so installed `continuum-*.yml` callers reference the fixed ref. See
[Install](README.md#install) and [Upgrading](README.md#upgrading-and-safe-pruning).

## Reporting a vulnerability

**Private vulnerability reporting is currently disabled for this repository
(verified 2026-10-22).** There is therefore no confidential reporting channel
through GitHub at this time. Do not open a public issue or post exploit details,
tokens, or consumer data in public.

To enable the supported private channel, a repository administrator must:

1. Open the repository's **Settings**.
2. In the sidebar, open **Code security and analysis** (under **Security**).
3. Find **Private vulnerability reporting** and click **Enable**.

GitHub's documentation:
<https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository>

After enablement, use the **Report a vulnerability** button on the repository's
**Security** tab. That button opens GitHub's private advisory form, which is
visible only to repository administrators and the reporter until an advisory is
published.

Do not send reports to a guessed or invented address. This repository publishes
no security email address.

## What to include

In a private report, include the affected workflow or file and ref, the impact,
reproduction steps that do not contain live secrets, and any known mitigation.
Never include live credentials (`TAP_PAT`, `RENDER_API_KEY`,
`CHILD_RUNTIME_TOKEN`, or any other token), consumer source, or third-party
personal data. If a credential may have been exposed, revoke it first and state
that it was revoked without reproducing the value.

## Scope

This policy covers the Continuum-owned files in this repository: reusable
workflows under `.github/workflows/`, caller templates under
`.github/caller-stubs/`, the installer, the Python engine under
`src/continuum/`, and helper scripts under `.github/scripts/` and `scripts/`.

Continuum executes in a consumer repository with that repository's permissions.
It is not a sandbox for untrusted code, and it cannot make an insecure consumer
configuration safe. Relevant credentials and trust boundaries are documented in:

- [Secrets and variables](README.md#secrets-and-variables);
- [Consumer configuration variables](docs/consumer-variables.md);
- [Parent/child delegated execution](docs/parent-child-delegation.md);
- [Commit and review provenance](docs/commit-review-provenance.md).

Third-party Actions, tools, and models invoked by a workflow remain under their
own licenses and security policies; report their issues to their respective
maintainers. Derived workflow material is attributed in [NOTICE](NOTICE).

Out of scope: vulnerabilities that require the attacker to already control the
consumer repository or its administrator-configured secrets, spam or
denial-of-service against GitHub itself, and reports about upstream services.

## No bounty or response guarantee

This project does not operate a bug bounty program and does not promise a
response time, fix timeline, or security certification.
