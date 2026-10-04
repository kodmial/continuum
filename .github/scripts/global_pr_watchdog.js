'use strict';

// Global cross-repository PR watchdog helper (kodmial/continuum#256).
//
// This module is a liveness wake-up layer only. It discovers Continuum
// consumer repositories, probes whether each has any open PR, and wakes the
// existing repository-local reconcilers. It owns no review/CI/merge
// semantics: those stay in continuum-auto-merge.yml,
// continuum-pr-agent-recovery.yml, and continuum-coderabbit-retry.yml.
//
// Privacy contract: per-repository diagnostics carry only an opaque
// repo_key derived from the repository id. Repository names, owners, URLs,
// and raw upstream error text must never reach logs or artifacts.
//
// The module is dependency-free (Node standard library only) so it can be
// required both from actions/github-script and from offline unit tests with
// an injected fake client.

const crypto = require('crypto');

const MAX_REPOSITORY_PAGES = 10;
const REPOS_PER_PAGE = 100;
const RATE_LIMIT_RECHECK_EVERY = 25;
const API_VERSION = '2026-03-10';
const ACCEPT_HEADER = 'application/vnd.github+json';

const ALLOWED_DISPATCH_WORKFLOWS = Object.freeze([
  'continuum-auto-merge.yml',
  'continuum-pr-agent-recovery.yml',
  'continuum-coderabbit-retry.yml',
]);

const DISPATCH_OPERATIONS = Object.freeze({
  'continuum-auto-merge.yml': 'dispatch_auto_merge',
  'continuum-pr-agent-recovery.yml': 'dispatch_pr_agent_recovery',
  'continuum-coderabbit-retry.yml': 'dispatch_coderabbit_retry',
});

const OPERATIONS = Object.freeze({
  RATE_LIMIT: 'rate_limit',
  DISCOVERY: 'discovery',
  CONSUMER_MARKER: 'consumer_marker',
  OPEN_PR_PROBE: 'open_pr_probe',
});

class HttpError extends Error {
  constructor(status, operation, headers) {
    super(`request failed status=${status} op=${operation}`);
    this.name = 'HttpError';
    this.status = status;
    this.operation = operation;
    this.headers = normalizeHeaders(headers);
  }
}

function normalizeHeaders(headers) {
  const normalized = {};
  if (!headers || typeof headers !== 'object') return normalized;
  for (const [key, value] of Object.entries(headers)) {
    normalized[String(key).toLowerCase()] = value;
  }
  return normalized;
}

// Shared API budget low-water mark: never start (or continue) discovery at
// or below the floor, so a watchdog pass cannot starve event-driven work.
function lowWaterMark(limit) {
  const numeric = Number(limit);
  if (!Number.isFinite(numeric) || numeric <= 0) return 500;
  return Math.max(500, Math.ceil(numeric * 0.10));
}

// Opaque per-repository log id. Only this value may appear in diagnostics.
function repoKey(repositoryId) {
  return crypto
    .createHash('sha256')
    .update(String(repositoryId), 'utf8')
    .digest('hex')
    .slice(0, 12);
}

function buildHeaders(token) {
  return {
    Accept: ACCEPT_HEADER,
    'X-GitHub-Api-Version': API_VERSION,
    Authorization: `Bearer ${token}`,
  };
}

function rateLimitPath() {
  return '/rate_limit';
}

function listReposPath(page) {
  return `/user/repos?per_page=${REPOS_PER_PAGE}&page=${Number(page)}&sort=pushed&direction=desc`;
}

function consumerMarkerPath(owner, repo, ref) {
  return `/repos/${owner}/${repo}/contents/.github/workflows/continuum-auto-merge.yml?ref=${encodeURIComponent(ref)}`;
}

function openPrProbePath(owner, repo) {
  return `/repos/${owner}/${repo}/pulls?state=open&sort=updated&direction=desc&per_page=1&page=1`;
}

function dispatchPath(owner, repo, workflowFile) {
  return `/repos/${owner}/${repo}/actions/workflows/${workflowFile}/dispatches`;
}

// Positive allowlist for every cross-repository request this watchdog may
// issue. Anything else is rejected before it is sent, which is what keeps a
// future edit from quietly adding a history scan or a direct PR mutation.
function isApprovedEndpoint(method, path) {
  if (typeof path !== 'string') return false;
  const upper = String(method || '').toUpperCase();
  if (upper === 'GET' && path === '/rate_limit') return true;
  if (upper === 'GET' && path.startsWith('/user/repos?')) return true;
  if (upper === 'GET' && path.includes('/contents/.github/workflows/continuum-auto-merge.yml?ref=')) {
    return path.startsWith('/repos/');
  }
  if (upper === 'GET' && path.includes('/pulls?state=open')) {
    return path.startsWith('/repos/');
  }
  if (upper === 'POST' && path.endsWith('/dispatches') && path.includes('/actions/workflows/')) {
    if (!path.startsWith('/repos/')) return false;
    return ALLOWED_DISPATCH_WORKFLOWS.some((file) => path.includes(`/actions/workflows/${file}/dispatches`));
  }
  return false;
}

function assertApprovedEndpoint(method, path) {
  if (!isApprovedEndpoint(method, path)) {
    throw new Error(`global-pr-watchdog: refusing unapproved cross-repository endpoint ${method} ${redactedPath(path)}`);
  }
}

// Never echo a full path (it carries repository names) in an error.
function redactedPath(path) {
  const text = String(path || '');
  const query = text.indexOf('?');
  return query === -1 ? '<path>' : `<path>?${text.slice(query + 1)}`;
}

// A repository is a Continuum core consumer only when its installed caller
// references this Continuum repository's reusable auto-merge workflow. The
// expected source is derived from the watchdog's own repository, never from
// a configured consumer name.
function consumerMarkerReference(continuumRepo) {
  return `${continuumRepo}/.github/workflows/continuum-auto-merge.yml@`;
}

function isContinuumConsumerFile(text, continuumRepo) {
  if (typeof text !== 'string' || !continuumRepo) return false;
  if (!text.includes('uses:')) return false;
  return text.includes(consumerMarkerReference(continuumRepo));
}

function ownerLoginOf(repo) {
  if (!repo) return '';
  if (typeof repo.owner === 'string') return repo.owner;
  if (repo.owner && typeof repo.owner.login === 'string') return repo.owner.login;
  return '';
}

function repoNameOf(repo) {
  if (!repo) return '';
  if (typeof repo.name === 'string') return repo.name;
  return '';
}

function defaultBranchOf(repo) {
  if (!repo) return 'main';
  const branch = repo.default_branch || repo.defaultBranch;
  if (typeof branch === 'string' && branch.trim()) return branch.trim();
  return 'main';
}

function isSkippedRepository(repo, continuumRepoId) {
  if (!repo || typeof repo !== 'object') return true;
  if (Number(repo.id) === Number(continuumRepoId)) return true;
  if (repo.archived === true || repo.disabled === true) return true;
  return !hasDispatchAccess(repo);
}

// The list endpoint reports repository permissions. Without push-level
// access a dispatch cannot succeed, so skip before spending further calls.
function hasDispatchAccess(repo) {
  const permissions = repo ? repo.permissions : undefined;
  if (!permissions || typeof permissions !== 'object') return true;
  return permissions.push === true || permissions.admin === true || permissions.maintain === true;
}

// Primary budget exhaustion: a 403 whose remaining quota header is zero, or
// any 429. The pass stops immediately with success; the next scheduled pass
// resumes when due. No waiting, no second attempts in the same run.
function isRateLimitStop(error) {
  if (!error || typeof error !== 'object') return false;
  const status = Number(error.status);
  if (status === 429) return true;
  if (status === 403) {
    const remaining = error.headers ? error.headers['x-ratelimit-remaining'] : undefined;
    return String(remaining) === '0';
  }
  return false;
}

function isInvalidCredential(error) {
  return error && Number(error.status) === 401;
}

function isForbiddenConfiguration(error) {
  return error && Number(error.status) === 403 && !isRateLimitStop(error);
}

function emptyCounters() {
  return {
    repositories_seen: 0,
    consumers_seen: 0,
    consumers_with_open_pr: 0,
    reconciler_dispatches: 0,
    missing_reconciler: 0,
    skipped_low_rate_budget: 0,
    request_failures_by_status: {},
  };
}

function noteFailure(counters, status) {
  const key = String(status === undefined || status === null ? 'unknown' : status);
  counters.request_failures_by_status[key] = (counters.request_failures_by_status[key] || 0) + 1;
}

function nullLog() {
  return { info() {}, warn() {} };
}

// Runs one bounded global pass. The client performs the approved REST calls
// and throws HttpError on HTTP failures:
//
//   getRateLimit() -> { limit, remaining, reset }
//   listRepositories(page) -> array of repository records
//   getConsumerMarker(owner, repo, ref) -> raw file text (throws HttpError)
//   probeOpenPulls(owner, repo) -> true when at least one PR is open
//   dispatchWorkflow(owner, repo, workflowFile, ref) -> void
//
// Never throws with repository-identifying detail. Fatal errors (invalid
// central credential, wrong permissions) throw a generic Error; every other
// cross-repository failure is counted and the pass continues or stops with
// success as documented.
async function runScan(client, options) {
  const settings = options || {};
  const continuumRepo = settings.continuumRepo;
  const continuumRepoId = settings.continuumRepoId;
  const log = settings.log || nullLog();
  if (!continuumRepo) {
    throw new Error('global-pr-watchdog: continuum repository is not configured');
  }
  const counters = emptyCounters();

  let guard;
  try {
    guard = await client.getRateLimit();
  } catch (error) {
    if (isRateLimitStop(error)) {
      log.info('global-pr-watchdog stopped: shared rate budget exhausted');
      return { counters, truncatedDiscovery: false, stoppedRateLimited: true, lowBudget: false };
    }
    failCredential(error);
    throw new Error('global-pr-watchdog: rate-limit guard is unavailable');
  }
  if (Number(guard.remaining) <= lowWaterMark(guard.limit)) {
    counters.skipped_low_rate_budget += 1;
    log.info(
      `global-pr-watchdog skipped: shared API budget low ` +
        `(remaining=${Number(guard.remaining)} low_water=${lowWaterMark(guard.limit)})`
    );
    return { counters, truncatedDiscovery: false, stoppedRateLimited: false, lowBudget: true };
  }

  let inspected = 0;
  let truncatedDiscovery = false;
  let stoppedRateLimited = false;
  let lowBudget = false;

  function failCredential(error) {
    if (isInvalidCredential(error)) {
      throw new Error('global-pr-watchdog: central credential is invalid');
    }
    if (isForbiddenConfiguration(error)) {
      throw new Error('global-pr-watchdog: credential lacks required access');
    }
  }

  function stopOnExhaustion() {
    stoppedRateLimited = true;
    log.info('global-pr-watchdog stopped: shared rate budget exhausted');
  }

  // Inspects one eligible repository. Returns true when the whole pass must
  // stop because the shared budget was exhausted mid-repository.
  async function inspectOne(repo) {
    const key = repoKey(repo.id);
    const owner = ownerLoginOf(repo);
    const name = repoNameOf(repo);
    const ref = defaultBranchOf(repo);

    let markerText;
    try {
      markerText = await client.getConsumerMarker(owner, name, ref);
    } catch (error) {
      if (isRateLimitStop(error)) {
        stopOnExhaustion();
        return true;
      }
      failCredential(error);
      // A missing caller file means "not a core consumer": the normal
      // single-call negative path, not a failure.
      if (error && Number(error.status) === 404) return false;
      noteFailure(counters, error ? error.status : undefined);
      log.warn(`global-pr-watchdog repo=${key} op=${OPERATIONS.CONSUMER_MARKER} status=${error ? error.status : 'unknown'}`);
      return false;
    }
    if (!isContinuumConsumerFile(markerText, continuumRepo)) return false;
    counters.consumers_seen += 1;

    let hasOpen;
    try {
      hasOpen = await client.probeOpenPulls(owner, name);
    } catch (error) {
      if (isRateLimitStop(error)) {
        stopOnExhaustion();
        return true;
      }
      failCredential(error);
      noteFailure(counters, error ? error.status : undefined);
      log.warn(`global-pr-watchdog repo=${key} op=${OPERATIONS.OPEN_PR_PROBE} status=${error ? error.status : 'unknown'}`);
      return false;
    }
    if (!hasOpen) return false;
    counters.consumers_with_open_pr += 1;

    for (const workflowFile of ALLOWED_DISPATCH_WORKFLOWS) {
      const operation = DISPATCH_OPERATIONS[workflowFile];
      try {
        await client.dispatchWorkflow(owner, name, workflowFile, ref);
        counters.reconciler_dispatches += 1;
      } catch (error) {
        if (isRateLimitStop(error)) {
          stopOnExhaustion();
          return true;
        }
        failCredential(error);
        // An older or incomplete installation may lack an optional
        // recovery caller. Count it and wake the remaining reconcilers.
        if (error && Number(error.status) === 404) {
          counters.missing_reconciler += 1;
          log.warn(`global-pr-watchdog repo=${key} op=${operation} status=404`);
          continue;
        }
        noteFailure(counters, error ? error.status : undefined);
        log.warn(`global-pr-watchdog repo=${key} op=${operation} status=${error ? error.status : 'unknown'}`);
      }
    }
    return false;
  }

  for (let page = 1; page <= MAX_REPOSITORY_PAGES; page += 1) {
    let repos;
    try {
      repos = await client.listRepositories(page);
    } catch (error) {
      if (isRateLimitStop(error)) {
        stopOnExhaustion();
        break;
      }
      failCredential(error);
      noteFailure(counters, error ? error.status : undefined);
      log.warn(`global-pr-watchdog op=${OPERATIONS.DISCOVERY} status=${error ? error.status : 'unknown'}`);
      continue;
    }
    if (!Array.isArray(repos) || repos.length === 0) break;
    if (page === MAX_REPOSITORY_PAGES && repos.length === REPOS_PER_PAGE) {
      truncatedDiscovery = true;
      log.warn('global-pr-watchdog discovery truncated at the bounded page cap');
    }

    let stopScan = false;
    for (const repo of repos) {
      counters.repositories_seen += 1;
      if (isSkippedRepository(repo, continuumRepoId)) continue;

      if (await inspectOne(repo)) {
        stopScan = true;
        break;
      }
      inspected += 1;
      if (inspected % RATE_LIMIT_RECHECK_EVERY === 0) {
        let check;
        try {
          check = await client.getRateLimit();
        } catch (error) {
          if (isRateLimitStop(error)) {
            stopOnExhaustion();
            stopScan = true;
            break;
          }
          failCredential(error);
          log.info('global-pr-watchdog stopped: budget recheck unavailable');
          stopScan = true;
          break;
        }
        if (Number(check.remaining) <= lowWaterMark(check.limit)) {
          counters.skipped_low_rate_budget += 1;
          lowBudget = true;
          log.info(
            `global-pr-watchdog stopped: shared API budget low ` +
              `(remaining=${Number(check.remaining)} low_water=${lowWaterMark(check.limit)})`
          );
          stopScan = true;
          break;
        }
      }
    }
    if (stopScan) break;
    if (repos.length < REPOS_PER_PAGE) break;
  }

  log.info(`global-pr-watchdog summary ${JSON.stringify(counters)}`);
  return { counters, truncatedDiscovery, stoppedRateLimited, lowBudget };
}

module.exports = {
  ACCEPT_HEADER,
  ALLOWED_DISPATCH_WORKFLOWS,
  API_VERSION,
  DISPATCH_OPERATIONS,
  OPERATIONS,
  MAX_REPOSITORY_PAGES,
  RATE_LIMIT_RECHECK_EVERY,
  REPOS_PER_PAGE,
  HttpError,
  assertApprovedEndpoint,
  buildHeaders,
  consumerMarkerPath,
  consumerMarkerReference,
  defaultBranchOf,
  dispatchPath,
  hasDispatchAccess,
  isApprovedEndpoint,
  isContinuumConsumerFile,
  isForbiddenConfiguration,
  isInvalidCredential,
  isRateLimitStop,
  isSkippedRepository,
  listReposPath,
  lowWaterMark,
  openPrProbePath,
  rateLimitPath,
  repoKey,
  runScan,
};
