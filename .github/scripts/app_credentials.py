#!/usr/bin/env python3
"""Resolve the least-privilege GitHub credential a job actually needs.

Continuum used to hand one long-lived personal access token (``TAP_PAT``) to
every privileged step. That credential was scoped ``repo + workflow``: it could
read and write every repository its owner could, for as long as it was never
rotated. Nothing about a job's actual needs was expressed anywhere, so the
blast radius of a leak was the owner's entire account rather than the one
capability the step performed.

This module replaces that coupling with a *capability* model:

* A **capability** is a named set of repository permissions that exactly one
  kind of operation needs. The registry below is the whole inventory, and every
  entry states which credential serves it and why the cheaper one cannot.
* The default credential is the job's own ``github.token``, whose authority is
  exactly the job's declared ``permissions`` block and which dies with the job.
  Most capabilities are served by it.
* Three capabilities cannot be. ``github.token`` cannot give an operation a
  repository identity the agent workflow's author gate will accept, and an event
  it creates does not start a normal CI run on a pull request. Those are served
  by a **GitHub App installation token**: short-lived (one hour), scoped to a
  single repository, and minted with exactly the permissions of the one
  capability that needs it.
* A personal access token is retained only as an explicit, opt-in migration
  fallback. It is never selected silently: the caller must set
  ``CONTINUUM_ALLOW_PAT_FALLBACK=true``, and the run says loudly that it did.

The minting path deliberately does not use an action from the marketplace. The
whole point of the change is that the credential's authority is auditable from
this file, so the token is minted here, in standard-library Python plus the
``openssl`` binary every runner already has, and the granted permission set is
compared against the requested one before the token is ever used.

The module also refuses to mint for a capability that ``github.token`` already
satisfies. That refusal is the control: "use the scoped ambient credential
wherever it is sufficient" is only true if the alternative is unavailable, and
a repository that reaches for a longer-lived credential has to change this
registry in a reviewed pull request rather than edit a workflow.

Standard library only, so it runs on stock ``ubuntu-latest`` runners.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Capability registry
# --------------------------------------------------------------------------- #

#: Which credential serves a capability.
DEFAULT_TOKEN = "github-token"
APP_TOKEN = "app-token"

#: GitHub expires every installation access token after one hour. A capability
#: whose job can outlast that needs a credential that does not, which is the
#: whole of the documented personal-access-token fallback.
INSTALLATION_TOKEN_MAX_LIFETIME_SECONDS = 3600


@dataclasses.dataclass(frozen=True)
class Capability:
    """One operation's worth of authority, and the argument for it."""

    name: str
    #: Repository permissions requested from the API, in REST spelling.
    permissions: dict
    #: ``github-token`` when the job's own ``github.token`` is sufficient.
    credential: str
    #: Why that credential, in one sentence. Nothing else may hold a credential
    #: without saying the same thing here.
    reason: str
    #: True when the token is handed to the model rather than to trusted code.
    #: A capability that may reach the model may never carry issue, Actions,
    #: or Administration authority.
    exposed_to_model: bool = False


CAPABILITIES = {
    capability.name: capability
    for capability in (
        Capability(
            name="read-only",
            permissions={
                "contents": "read",
                "issues": "read",
                "pull_requests": "read",
                "actions": "read",
            },
            credential=DEFAULT_TOKEN,
            reason=(
                "Control planes decide what to trust and act on nothing. A "
                "verification step that could write would let an attacker "
                "authorise its own request."
            ),
        ),
        Capability(
            name="issue-write",
            permissions={"contents": "read", "issues": "write", "pull_requests": "read"},
            credential=APP_TOKEN,
            reason=(
                "The scheduler's /oc comment must be authored by an account the "
                "agent workflow's author gate accepts. github.token acts as "
                "github-actions[bot], which that gate refuses by design, so the "
                "dispatch would consume a reservation and start nothing."
            ),
        ),
        Capability(
            name="pull-request-write",
            permissions={"contents": "read", "issues": "read", "pull_requests": "write"},
            credential=DEFAULT_TOKEN,
            reason=(
                "Recording a repair outcome, releasing a lock, and commenting on "
                "a pull request need nothing beyond the job's declared scopes."
            ),
        ),
        Capability(
            name="contents-write",
            permissions={"contents": "write", "pull_requests": "read"},
            credential=DEFAULT_TOKEN,
            reason=(
                "An ordinary branch push. The workflow-file mutation capability is "
                "separate, so this one is never granted `workflows`."
            ),
        ),
        Capability(
            name="workflow-files-write",
            permissions={"contents": "write", "workflows": "write", "pull_requests": "read"},
            credential=DEFAULT_TOKEN,
            reason=(
                "Updating .github/workflows/** needs the `workflows` scope. It is a "
                "separate capability so that a push which cannot touch the "
                "privilege boundary is not silently able to."
            ),
        ),
        Capability(
            name="actions-dispatch",
            permissions={"actions": "write", "contents": "read", "pull_requests": "read"},
            credential=DEFAULT_TOKEN,
            reason=(
                "Re-running a failed run and waking a workflow are the two things "
                "github.token is documented to trigger normally. The dispatch "
                "happens; what it cannot do is arrive as a sender the receiving "
                "workflow's author gate accepts, because it acts as "
                "github-actions[bot]. A step that needs a *trusted sender* at the "
                "other end therefore needs the app token, and declares that "
                "capability under `repair-control` instead."
            ),
        ),
        Capability(
            name="merge",
            permissions={"contents": "write", "issues": "read", "pull_requests": "write"},
            credential=DEFAULT_TOKEN,
            reason=(
                "Squash-merging an approved pull request is exactly the job's "
                "declared contents and pull-requests write scope."
            ),
        ),
        Capability(
            name="repair-control",
            permissions={
                "actions": "write",
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
            },
            credential=APP_TOKEN,
            reason=(
                "Two structural reasons, either of which alone is enough. The "
                "repair controller dispatches the agent workflow, whose author gate "
                "refuses a github-actions[bot] sender; and a branch update made "
                "with github.token produces a pull_request run in approval-required "
                "state with zero jobs, so the updated head would never get a real "
                "CI run and the pull request would stall forever."
            ),
        ),
        Capability(
            name="agent-authoring",
            permissions={
                "contents": "write",
                "issues": "read",
                "pull_requests": "write",
                # Without this, a push that touches .github/workflows/** is
                # rejected. An agent that could not fix its own pipeline would
                # have to stop and ask a human for every workflow change.
                "workflows": "write",
            },
            credential=APP_TOKEN,
            reason=(
                "The agent pushes its own branch and opens its own pull request. "
                "That event has to start CI automatically, which github.token "
                "cannot do for a pull request it created itself. It carries "
                "neither issues nor actions authority: a compromised model may "
                "push code and comment on its pull request, and nothing else."
            ),
            exposed_to_model=True,
        ),
    )
}

#: Capabilities whose events or identity the job's own token cannot produce.
#: Every non-default credential in this repository is justified by one of these,
#: and ``docs/credential-capabilities.md`` is generated from the same registry.
APP_TOKEN_CAPABILITIES = frozenset(
    name for name, capability in CAPABILITIES.items() if capability.credential == APP_TOKEN
)

#: Scopes a model-facing token may never hold *write* on. Reading is fine -- the
#: agent has to read the issue it is working on -- but a leaked model credential
#: must not be able to move labels, dispatch workflows, or administer the
#: repository. Read access to a public tracker is not a capability worth hiding;
#: write access to one is the whole of the control plane.
MODEL_FORBIDDEN_PERMISSIONS = ("issues", "actions", "administration", "organization_admin")

#: Scopes no model-facing token may hold at all, at any level.
MODEL_ABSOLUTE_FORBIDDEN_PERMISSIONS = ("organization_admin",)


class CredentialError(RuntimeError):
    """Every way credential resolution can refuse. Never falls back silently."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def capability(name: str) -> Capability:
    known = ", ".join(sorted(CAPABILITIES))
    if name not in CAPABILITIES:
        raise CredentialError(
            "Unknown capability {!r}. Known capabilities: {}.".format(name, known)
        )
    return CAPABILITIES[name]


def env_name_for(name: str) -> str:
    """The environment variable a minted capability token is published under.

    Derived from the capability name so a workflow cannot publish one
    capability's token under another capability's variable name.
    """
    return "CONTINUUM_TOKEN_" + name.upper().replace("-", "_")


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalize_private_key(raw: str) -> str:
    """Accept the PEM in the two shapes a repository secret arrives in.

    A secret set through the API or copied from a file often arrives with
    literal ``\\n`` sequences instead of real newlines. Both must produce the
    same key, and a key that is neither is a refusal rather than a guess.
    """
    text = (raw or "").strip()
    if "-----BEGIN" not in text:
        raise CredentialError(
            "The GitHub App private key secret is not a PEM key. Set the "
            "repository secret CONTINUUM_APP_PRIVATE_KEY to the .pem the App "
            "settings page generated."
        )
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def api_request(method: str, path: str, token: str, body=None, *, api_url: str = "") -> dict:
    """One authenticated call. Returns ``{}`` for an empty body."""
    if not token:
        raise CredentialError("A credential is required for the GitHub API.")
    base = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "continuum-app-credentials",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + path, data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise CredentialError(
            "GitHub rejected {} {} ({}): {}".format(method, path, exc.code, detail)
        ) from exc
    except urllib.error.URLError as exc:
        raise CredentialError("GitHub API request failed: {}".format(exc.reason)) from exc
    return json.loads(raw) if raw else {}


def sign_jwt(private_key_pem: str, app_id: str, *, lifetime_seconds: int = 540) -> str:
    """Build the RS256 JSON Web Token that authenticates as the App itself.

    The token is deliberately short (nine minutes): it exists only to exchange
    one installation token, so a leak is worthless almost immediately. The
    signature is produced by ``openssl``, which is the only RSA signer on a
    stock runner, and the private key is written to a 0600 temporary file that
    is removed before this function returns.
    """
    issued_at = int(time.time()) - 30
    header = base64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    claims = base64url(
        json.dumps(
            {
                "iat": issued_at,
                "exp": issued_at + int(lifetime_seconds),
                "iss": str(app_id),
            }
        ).encode("utf-8")
    )
    signing_input = "{}.{}".format(header, claims).encode("ascii")

    handle, path = tempfile.mkstemp(prefix="continuum-app-key-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as key_file:
            key_file.write(private_key_pem)
        os.chmod(path, 0o600)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", path],
            input=signing_input,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise CredentialError(
            "openssl is required to sign the GitHub App assertion and is not on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise CredentialError(
            "The GitHub App private key could not be signed with: {}".format(
                exc.stderr.decode("utf-8", "replace")[:300]
            )
        ) from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    return "{}.{}".format(signing_input.decode("ascii"), base64url(result.stdout))


# --------------------------------------------------------------------------- #
# Permission discipline
# --------------------------------------------------------------------------- #


def permission_levels(permissions: dict) -> dict:
    """Normalize an API permission map to lowercase ``{scope: level}``."""
    if not isinstance(permissions, dict):
        return {}
    normalized = {}
    for scope, level in permissions.items():
        if not isinstance(scope, str) or not isinstance(level, str):
            continue
        scope = scope.strip().lower().replace(" ", "_").replace("-", "_")
        level = level.strip().lower()
        if scope and level:
            normalized[scope] = level
    return normalized


def overgranted(requested: dict, granted: dict) -> list:
    """Scopes the token holds that the caller did not ask for, or holds higher.

    The mint endpoint narrows an installation token to the requested set, so a
    difference here means the credential is not the credential this capability
    was audited against. Fail closed instead of using it.
    """
    wanted = permission_levels(requested)
    actual = permission_levels(granted)
    order = {"none": 0, "read": 1, "write": 2, "admin": 3}
    extra = []
    for scope, level in sorted(actual.items()):
        if scope not in wanted:
            # `metadata` is implicit on every installation token.
            if scope == "metadata":
                continue
            extra.append("{}={} (not requested)".format(scope, level))
            continue
        if order.get(level, 0) > order.get(wanted[scope], 0):
            extra.append("{}={} (requested {})".format(scope, level, wanted[scope]))
    return extra


def assert_least_privilege(requested: dict, granted: dict) -> None:
    extra = overgranted(requested, granted)
    if extra:
        raise CredentialError(
            "The minted token holds authority beyond its capability: {}. Refusing "
            "to use a credential that is not the one that was audited.".format(
                ", ".join(extra)
            )
        )

def assert_capability_shape(name: str) -> None:
    """Reject a registry entry that contradicts its own stated discipline.

    A capability the model may read must not carry authority a model has no
    business with, and a capability that cannot say why it exists is an
    unreviewed credential.
    """
    entry = capability(name)
    if entry.exposed_to_model:
        levels = permission_levels(entry.permissions)
        forbidden = sorted(
            scope
            for scope, level in levels.items()
            if scope in MODEL_ABSOLUTE_FORBIDDEN_PERMISSIONS
            or (scope in MODEL_FORBIDDEN_PERMISSIONS and level == "write")
        )
        if forbidden:
            raise CredentialError(
                "Capability {!r} is exposed to the model but grants {}. A model "
                "credential must not be able to move labels, dispatch workflows, "
                "or administer the repository.".format(name, ", ".join(forbidden))
            )
    if not entry.reason.strip():
        raise CredentialError(
            "Capability {!r} has no stated reason. A non-obvious credential that "
            "cannot say why it exists is an unreviewed credential.".format(name)
        )


def assert_registry() -> None:
    """Reject a registry that has stopped being a policy.

    Two properties make the table mean something, and neither is visible in a
    single entry:

    * at least one capability must be served by the ambient ``github.token``.
      "GITHUB_TOKEN wherever sufficient" is a claim about the whole registry, so
      a table where everything needs a minted credential means the claim stopped
      being true quietly.
    * every non-default credential must justify itself *against the ambient
      token* -- the reason has to name what ``github.token`` cannot do, because
      that comparison is the entire justification for holding a broader
      credential. "Needed for CI" is not a reason; "a synchronize event from
      github.token produces an approval-required run with zero jobs" is.
    """
    ambient = [name for name, entry in CAPABILITIES.items() if entry.credential == DEFAULT_TOKEN]
    if not ambient:
        raise CredentialError(
            "No capability is served by github.token. Every credential in this "
            "registry is minted, which means the ambient token is not being used "
            "where it is sufficient."
        )
    for name, entry in sorted(CAPABILITIES.items()):
        assert_capability_shape(name)
        if entry.credential == APP_TOKEN and "github.token" not in entry.reason:
            raise CredentialError(
                "Capability {!r} requires a minted credential, so its reason must "
                "say what github.token cannot do for it.".format(name)
            )



# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def repository_from(value: str = "") -> tuple:
    repository = (value or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if repository.count("/") != 1 or repository.startswith("/") or repository.endswith("/"):
        raise CredentialError(
            "GITHUB_REPOSITORY must be owner/repo; got {!r}.".format(repository)
        )
    owner, name = repository.split("/", 1)
    if not owner or not name:
        raise CredentialError("GITHUB_REPOSITORY must be owner/repo; got {!r}.".format(repository))
    return owner, name


def pat_fallback_enabled() -> bool:
    return (os.environ.get("CONTINUUM_ALLOW_PAT_FALLBACK") or "").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def pat_fallback_token() -> str:
    if not pat_fallback_enabled():
        raise CredentialError(
            "No GitHub App is configured, so capability {name!r} cannot be "
            "satisfied by this repository's ambient github.token. "
            "TAP_PAT exists only as a migration fallback and is refused unless it "
            "is opted into: set the repository variable CONTINUUM_ALLOW_PAT_FALLBACK "
            "to 'true' to use it during migration, or create and install the "
            "Continuum GitHub App (repository variable CONTINUUM_APP_ID and secret "
            "CONTINUUM_APP_PRIVATE_KEY) and set CONTINUUM_AUTOMATION_LOGIN to the "
            "App's bot login. See docs/credential-capabilities.md."
        )
    token = (os.environ.get("TAP_PAT") or "").strip()
    if not token:
        raise CredentialError(
            "CONTINUUM_ALLOW_PAT_FALLBACK is enabled but the TAP_PAT secret is "
            "empty. Either configure the secret or remove the variable so the "
            "capability fails closed."
        )
    return token


def expected_identity() -> str:
    return (os.environ.get("CONTINUUM_AUTOMATION_LOGIN") or "").strip()


def app_identity(token: str, *, api_url: str = "") -> str:
    """The login an installation token speaks as: ``<slug>[bot]``."""
    data = api_request("GET", "/app", token, api_url=api_url)
    slug = data.get("slug") or ""
    if not isinstance(slug, str) or not slug.strip():
        raise CredentialError(
            "The GitHub App installation token does not identify an App. A user "
            "token cannot stand in for it: the automation identity is part of the "
            "capability."
        )
    return "{}[bot]".format(slug.strip().lower())


def mint_installation_token(name: str, repository: str, *, api_url: str = "") -> dict:
    """Mint one repository-scoped, capability-scoped installation token."""
    assert_registry()
    entry = capability(name)
    if entry.credential != APP_TOKEN:
        raise CredentialError(
            "Capability {name!r} is satisfied by the job's own github.token, whose "
            "authority is exactly the job's declared `permissions` block. Minting a "
            "longer-lived credential for it would replace a least-privilege "
            "credential with a broader one. Use ${{{{ github.token }}}} instead; "
            "if a genuinely new need appears, add the capability to the registry in "
            ".github/scripts/app_credentials.py with its reason.".format(name=name)
        )

    app_id = (os.environ.get("CONTINUUM_APP_ID") or "").strip()
    raw_key = os.environ.get("CONTINUUM_APP_PRIVATE_KEY") or ""
    if not app_id or not raw_key.strip():
        raise CredentialError(
            "Capability {name!r} needs a GitHub App installation token, and neither "
            "CONTINUUM_APP_ID nor CONTINUUM_APP_PRIVATE_KEY is set. {reason}".format(
                name=name, reason=entry.reason
            )
        )

    owner, repo = repository_from(repository)
    private_key = normalize_private_key(raw_key)
    assertion = sign_jwt(private_key, app_id)

    installation = api_request(
        "GET",
        "/repos/{}/{}/installation".format(owner, repo),
        assertion,
        api_url=api_url,
    )
    installation_id = installation.get("id")
    if not isinstance(installation_id, int) or installation_id <= 0:
        raise CredentialError(
            "The GitHub App is not installed on {}/{} ({}).".format(owner, repo, installation_id)
        )

    response = api_request(
        "POST",
        "/app/installations/{}/access_tokens".format(installation_id),
        assertion,
        body={
            "permissions": dict(entry.permissions),
            "repositories": [repo],
        },
        api_url=api_url,
    )
    token = response.get("token")
    if not isinstance(token, str) or not token.strip():
        raise CredentialError("GitHub returned an installation token with no token value.")
    token = token.strip()

    granted = response.get("permissions") or {}
    assert_least_privilege(entry.permissions, granted)

    identity = app_identity(token, api_url=api_url)
    wanted = expected_identity()
    if wanted and wanted.strip().lower() != identity.lower():
        raise CredentialError(
            "The minted credential speaks as @{got}, but this repository declares "
            "CONTINUUM_AUTOMATION_LOGIN=@{want}. The trust policy would refuse the "
            "dispatch anyway; refusing here names the real problem.".format(
                got=identity, want=wanted.strip()
            )
        )

    expires_at = response.get("expires_at") or ""
    return {
        "token": token,
        "identity": identity,
        "kind": "app-installation",
        "expires_at": str(expires_at),
        "source": "github-app",
        "capability": name,
        "permissions": permission_levels(granted),
    }


def resolve(name: str, repository: str = "", *, api_url: str = "") -> dict:
    """Resolve the credential for ``name``, preferring the App to the PAT.

    The App is always tried first and the personal access token only ever
    behind an explicit opt-in, so a repository that has finished migrating
    cannot drift back to a long-lived credential because of a transient
    failure.
    """
    try:
        return mint_installation_token(name, repository, api_url=api_url)
    except CredentialError as exc:
        if not pat_fallback_enabled():
            raise
        detail = str(exc)
        if not ("CONTINUUM_APP_ID" in detail or "CONTINUUM_APP_PRIVATE_KEY" in detail):
            # A failure that is *not* "the App is unconfigured" -- a wrong
            # identity, an over-granted token, a revoked key -- must not be
            # papered over by a broader credential.
            raise
        print(
            "::warning::Continuing with the documented personal access token "
            "fallback because CONTINUUM_ALLOW_PAT_FALLBACK is enabled. Remove that "
            "variable once the GitHub App is installed: " + one_line(detail),
            file=sys.stderr,
        )
        return {
            "token": pat_fallback_token(),
            "identity": "",
            "kind": "user-pat",
            "expires_at": "",
            "source": "pat-fallback",
            "capability": name,
            "permissions": {},
        }


def one_line(text: str, limit: int = 400) -> str:
    return " ".join(str(text or "").split())[:limit]


#: ``GITHUB_OUTPUT`` key -> key in the resolved credential.
PUBLISHED_OUTPUTS = {
    "credential_source": "source",
    "credential_kind": "kind",
    "credential_identity": "identity",
    "credential_capability": "capability",
    "credential_expires_at": "expires_at",
}


def publish(resolved: dict, env_name: str) -> None:
    """Mask the token, export it, and write only non-secret metadata out."""
    token = resolved["token"]
    print("::add-mask::{}".format(token))

    destination = os.environ.get("GITHUB_ENV")
    if not destination:
        raise CredentialError("GITHUB_ENV is not set; refusing to leak a token to stdout.")
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write("{}={}\n".format(env_name, token))

    outputs = os.environ.get("GITHUB_OUTPUT")
    if outputs:
        with open(outputs, "a", encoding="utf-8") as handle:
            for output_key, field in PUBLISHED_OUTPUTS.items():
                handle.write("{}={}\n".format(output_key, one_line(resolved.get(field, ""))))
            handle.write(
                "credential_permissions={}\n".format(
                    ",".join(
                        "{}={}".format(scope, level)
                        for scope, level in sorted(resolved["permissions"].items())
                    )
                )
            )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def cmd_mint(args) -> int:
    name = capability(args.capability).name
    env_name = args.env_name or env_name_for(name)
    repository = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    resolved = resolve(name, repository, api_url=args.api_url)

    publish(resolved, env_name)

    print(
        "::notice::Capability '{capability}' resolved to a {kind} credential "
        "({source}) speaking as @{identity}, permissions {permissions}, expiring "
        "{expires}; published as {env_name}.".format(
            capability=name,
            kind=resolved["kind"],
            source=resolved["source"],
            identity=resolved["identity"] or "an unverified account (fallback)",
            permissions=", ".join(
                "{}={}".format(scope, level)
                for scope, level in sorted(resolved["permissions"].items())
            )
            or "declared by the fallback credential, not by this capability",
            expires=resolved["expires_at"] or "never (personal access token)",
            env_name=env_name,
        )
    )
    return 0


def cmd_capabilities(args) -> int:
    """Print the registry. The audit and the documentation both read this."""
    assert_registry()
    for name in sorted(CAPABILITIES):
        entry = CAPABILITIES[name]
        assert_capability_shape(name)
        print(
            "{name}\t{credential}\t{permissions}\t{model}\t{reason}".format(
                name=name,
                credential=entry.credential,
                permissions=",".join(
                    "{}={}".format(scope, level)
                    for scope, level in sorted(entry.permissions.items())
                ),
                model="model-exposed" if entry.exposed_to_model else "trusted-code",
                reason=entry.reason,
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve a least-privilege GitHub credential for one capability"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    mint = sub.add_parser("mint", help="Mint and publish one capability's credential")
    mint.add_argument("--capability", required=True)
    mint.add_argument("--env-name", dest="env_name", default="")
    mint.add_argument("--repository", default="")
    mint.add_argument("--api-url", dest="api_url", default="")
    mint.set_defaults(func=cmd_mint)

    listing = sub.add_parser("capabilities", help="Print the capability registry")
    listing.set_defaults(func=cmd_capabilities)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CredentialError as exc:
        print("::error::{}".format(one_line(exc, limit=1000)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
