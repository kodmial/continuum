'use strict';

// Canonical provider-neutral merge lifecycle (kodmial/continuum#304).
//
// Single runtime implementation of the shared lifecycle owned equally by
// the CodeRabbit path (continuum-auto-merge.yml) and the PR-Agent path
// (continuum-pr-agent-auto-merge.yml). Mirrors
// src/continuum/merge_lifecycle.py decision-for-decision; the Python
// module is authoritative for tests while this bundle is authoritative
// at workflow runtime. Provider-specific retry/review policy must never
// be added here: CodeRabbit semantics live in coderabbit_adapter.py and
// PR-Agent semantics live in pr_agent_lifecycle.py / pr_agent_policy.js.
//
// This bundle introduces no new controller, reducer, or lifecycle
// authority: it exports pure helpers only. The two auto-merge workflows
// remain the sole executors.

const AUTO_MERGE_BLOCK_LABEL = 'no-auto-merge';
const CONFLICT_LOCK_LABEL = 'opencode-conflict-repair';
const REVIEW_PROVIDERS = ['none', 'coderabbit', 'pr-agent'];
const CONVENTIONAL_TITLE_RE = /^(?:fix|feat|perf|refactor)(?:\([^)]*\))?!?:\s/i;
const BRANCH_SANITIZE_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/;

function resolveReviewProvider(value) {
  const text = String(value || 'none').trim().toLowerCase() || 'none';
  if (!REVIEW_PROVIDERS.includes(text)) {
    throw new Error(
      'invalid CONTINUUM_REVIEW_PROVIDER: expected none, coderabbit, or pr-agent'
    );
  }
  return text;
}

function isSameHead(expectedSha, actualSha) {
  const expected = String(expectedSha || '').trim().toLowerCase();
  const actual = String(actualSha || '').trim().toLowerCase();
  if (!expected || !actual) return false;
  return expected === actual;
}

function validateExactHead(reviewedHead, liveHead) {
  if (!String(reviewedHead || '').trim()) {
    return { ok: false, reason: 'reviewed HEAD is missing: failing closed' };
  }
  if (!String(liveHead || '').trim()) {
    return { ok: false, reason: 'live PR HEAD is missing: failing closed' };
  }
  if (!isSameHead(reviewedHead, liveHead)) {
    return {
      ok: false,
      reason:
        'PR head moved (' +
        String(reviewedHead).trim().toLowerCase() +
        ' -> ' +
        String(liveHead).trim() +
        '): failing closed',
    };
  }
  return { ok: true, reason: 'exact HEAD verified' };
}

function labelNames(labels) {
  const names = new Set();
  for (const label of labels || []) {
    if (typeof label === 'string') names.add(label);
    else if (label && typeof label.name === 'string') names.add(label.name);
  }
  return names;
}

function isMergeBlocked(labels) {
  return labelNames(labels).has(AUTO_MERGE_BLOCK_LABEL);
}

function isNonMergeCriticalMainPath(path) {
  const text = String(path || '');
  if (text.startsWith('.github/workflows/continuum-')) return false;
  return (
    text === 'LICENSE' ||
    text === '.gitignore' ||
    text === '.release-please-manifest.json' ||
    text === 'CHANGELOG.md' ||
    text.endsWith('.md') ||
    text.startsWith('docs/') ||
    text.startsWith('.github/')
  );
}

function decideMainSync(behindBy, files) {
  const behind = Number(behindBy || 0);
  const names = Array.from(files || []).map((item) => String(item));
  if (!(behind > 0)) {
    return { required: false, reason: 'up-to-date', files: names, criticalFiles: [] };
  }
  const critical = names.filter((item) => !isNonMergeCriticalMainPath(item));
  if (names.length > 0 && critical.length === 0) {
    return {
      required: false,
      reason: 'non-merge-critical-main-delta',
      files: names,
      criticalFiles: critical,
    };
  }
  return {
    required: true,
    reason: names.length === 0 ? 'unknown-main-delta' : 'merge-critical-main-delta',
    files: names,
    criticalFiles: critical,
  };
}

function sanitizeBranch(name, fallback = 'main') {
  let value = String(name || '').trim() || String(fallback || '').trim() || 'main';
  if (!BRANCH_SANITIZE_RE.test(value)) return 'main';
  if (value.includes('..') || value.includes('//') || value.includes('@{')) return 'main';
  if (value.endsWith('/') || value.endsWith('.') || value.endsWith('.lock')) return 'main';
  if (value === 'HEAD' || value === '@') return 'main';
  if (value.startsWith('refs/') || value.startsWith('-') || value.startsWith('.')) return 'main';
  if (value.split('/').some((part) => part.startsWith('.') || part.endsWith('.lock'))) {
    return 'main';
  }
  return value;
}

function sanitizeWakeupRef(value, fallback = 'main') {
  return sanitizeBranch(value, fallback);
}

function parsePostMergeWakeups(value) {
  return String(value || '')
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

function normalizeMergeTitle(title) {
  const text = String(title || '').trim();
  if (!text) throw new Error('merge title must not be empty');
  if (CONVENTIONAL_TITLE_RE.test(text)) return text;
  return 'fix: ' + text;
}

function buildMergeParams({ reviewedHead, liveHead, title }) {
  const check = validateExactHead(reviewedHead, liveHead);
  if (!check.ok) throw new Error(check.reason);
  const sha = String(reviewedHead).trim().toLowerCase();
  if (!/^[0-9a-f]{7,64}$/.test(sha)) {
    throw new Error('reviewed HEAD is not a commit SHA: failing closed');
  }
  return { commit_title: normalizeMergeTitle(title), sha };
}

function leaseKey(prNumber, headSha) {
  return String(prNumber) + ':' + String(headSha || '').trim().toLowerCase();
}

function runGateState(run) {
  if (!run || typeof run !== 'object') return { status: 'missing', conclusion: 'missing' };
  return {
    status: String(run.status || 'missing'),
    conclusion: String(run.conclusion || 'pending'),
  };
}

function evaluatePackagingGate(packagingRun) {
  if (packagingRun === null || packagingRun === undefined) {
    return { ok: true, reason: 'packaging smoke absent: gate passes' };
  }
  const { status, conclusion } = runGateState(packagingRun);
  if (status !== 'completed' || conclusion !== 'success') {
    return { ok: false, reason: 'Packaging smoke is ' + status + '/' + conclusion };
  }
  return { ok: true, reason: 'packaging smoke successful' };
}

function evaluateRequiredWorkflowGate({ labels, gateLabel, gateName, requiredRun }) {
  const label = String(gateLabel || '').trim();
  const name = String(gateName || '').trim();
  if (!label || !name) return { ok: true, reason: 'required workflow gate not configured' };
  if (!labelNames(labels).has(label)) {
    return { ok: true, reason: 'required workflow gate label absent' };
  }
  if (requiredRun === null || requiredRun === undefined) {
    return { ok: false, reason: 'required workflow ' + name + ' is not successful' };
  }
  const { status, conclusion } = runGateState(requiredRun);
  if (status !== 'completed' || conclusion !== 'success') {
    return { ok: false, reason: 'required workflow ' + name + ' is not successful' };
  }
  return { ok: true, reason: 'required workflow gate successful' };
}

function evaluateCurrentHeadGates({
  ciRun,
  contractGateRun = null,
  contractQualificationRun = null,
  packagingRun = null,
  labels = [],
  requiredGateLabel = '',
  requiredGateName = '',
  requiredGateRun = null,
  repository = '',
  delegatedSkippedCi = false,
}) {
  if (ciRun === null || ciRun === undefined) {
    return { ok: false, reason: 'Current-head CI is not acceptable' };
  }
  const ci = runGateState(ciRun);
  if (ci.status !== 'completed' || (ci.conclusion !== 'success' && !delegatedSkippedCi)) {
    return { ok: false, reason: 'Current-head CI is not acceptable' };
  }
  if (String(repository || '') === 'kodmial/continuum') {
    for (const [run, gateName] of [
      [contractGateRun, 'Continuum Contract Gate'],
      [contractQualificationRun, 'Contract qualification'],
    ]) {
      if (run === null || run === undefined) {
        return { ok: false, reason: gateName + ' is not successful' };
      }
      const state = runGateState(run);
      if (state.status !== 'completed' || state.conclusion !== 'success') {
        return { ok: false, reason: gateName + ' is not successful' };
      }
    }
  }
  const packaging = evaluatePackagingGate(packagingRun);
  if (!packaging.ok) return packaging;
  return evaluateRequiredWorkflowGate({
    labels,
    gateLabel: requiredGateLabel,
    gateName: requiredGateName,
    requiredRun: requiredGateRun,
  });
}

module.exports = {
  AUTO_MERGE_BLOCK_LABEL,
  CONFLICT_LOCK_LABEL,
  REVIEW_PROVIDERS,
  buildMergeParams,
  decideMainSync,
  evaluateCurrentHeadGates,
  evaluatePackagingGate,
  evaluateRequiredWorkflowGate,
  isMergeBlocked,
  isNonMergeCriticalMainPath,
  isSameHead,
  leaseKey,
  normalizeMergeTitle,
  parsePostMergeWakeups,
  resolveReviewProvider,
  sanitizeBranch,
  sanitizeWakeupRef,
  validateExactHead,
};
