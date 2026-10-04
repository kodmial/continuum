'use strict';

const crypto = require('crypto');

const IMPROVE_REPAIR_THRESHOLD = 7;
const CONTROLLER_STATE_MARKER = '<!-- continuum-pr-agent-controller-state:v1 -->';

const PATH_KEYS = ['relevant_file', 'path', 'file', 'filename'];
const START_KEYS = ['relevant_lines_start', 'line_start', 'start_line', 'line'];
const END_KEYS = ['relevant_lines_end', 'line_end', 'end_line', 'line'];
const REVIEW_TEXT_KEYS = ['issue_header', 'issue_content', 'title', 'body', 'description'];
const IMPROVE_TEXT_KEYS = [
  'one_sentence_summary',
  'suggestion_content',
  'title',
  'body',
  'description',
  'label',
];

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, stable(value[key])])
    );
  }
  return value;
}

function parseImproveJsonl(raw) {
  const suggestions = [];
  for (const line of String(raw || '').split('\n')) {
    if (!line.trim()) continue;
    let record;
    try {
      record = JSON.parse(line);
    } catch (err) {
      throw new Error('Invalid improve push_outputs line: ' + err.message);
    }
    const payload = record && typeof record === 'object' ? (record.payload || record) : null;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('Invalid improve push_outputs payload object.');
    }
    const batch = payload.code_suggestions || [];
    if (!Array.isArray(batch)) {
      throw new Error('Invalid improve push_outputs code_suggestions list.');
    }
    for (const entry of batch) {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
        throw new Error('Invalid improve suggestion object.');
      }
      suggestions.push(entry);
    }
  }
  return suggestions;
}

function numericScore(entry) {
  const value = entry ? entry.score : undefined;
  if (value === undefined || value === null || value === '') return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function qualifyingImproveSuggestions(input, threshold = IMPROVE_REPAIR_THRESHOLD) {
  const suggestions = typeof input === 'string' ? parseImproveJsonl(input) : Array.from(input || []);
  const minimum = Number(threshold);
  if (!Number.isFinite(minimum)) {
    throw new Error('Invalid improve repair threshold.');
  }
  return suggestions.filter((entry) => {
    const score = numericScore(entry);
    return score !== null && score >= minimum;
  });
}

function firstString(entry, keys) {
  for (const key of keys) {
    const value = entry ? entry[key] : undefined;
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function firstNumber(entry, keys) {
  for (const key of keys) {
    const value = entry ? entry[key] : undefined;
    if (value === undefined || value === null || value === '') continue;
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return Math.trunc(numeric);
  }
  return null;
}

function normalizePath(entry) {
  return firstString(entry, PATH_KEYS)
    .replace(/^\.\//, '')
    .replace(/^[`"']+|[`"']+$/g, '')
    .trim()
    .toLowerCase();
}

function lineRange(entry) {
  let start = firstNumber(entry, START_KEYS);
  let end = firstNumber(entry, END_KEYS);
  if (start === null || end === null) {
    const raw = entry ? entry.relevant_lines : undefined;
    if (typeof raw === 'string') {
      const match = raw.match(/(\d+)\D+(\d+)/);
      if (match) {
        if (start === null) start = Number(match[1]);
        if (end === null) end = Number(match[2]);
      } else {
        const one = raw.match(/\d+/);
        if (one) {
          if (start === null) start = Number(one[0]);
          if (end === null) end = Number(one[0]);
        }
      }
    }
  }
  if (start !== null && end === null) end = start;
  if (end !== null && start === null) start = end;
  if (start !== null && end !== null && end < start) {
    const tmp = start;
    start = end;
    end = tmp;
  }
  return { start, end };
}

function normalizeProblem(entry, source) {
  const keys = source === 'review' ? REVIEW_TEXT_KEYS : IMPROVE_TEXT_KEYS;
  return keys
    .map((key) => (entry && typeof entry[key] === 'string' ? entry[key] : ''))
    .filter(Boolean)
    .join(' ')
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/[`*_>#()[\]{}]/g, ' ')
    .replace(/[^\p{L}\p{N}_./-]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

function tokenSet(text) {
  return new Set(
    String(text || '')
      .split(/\s+/)
      .map((token) => token.trim())
      .filter((token) => token.length >= 3)
  );
}

function equivalentProblem(left, right) {
  if (!left || !right) return false;
  if (left === right) return true;
  const shorter = left.length <= right.length ? left : right;
  const longer = shorter === left ? right : left;
  if (shorter.length >= 32 && longer.includes(shorter)) return true;

  const a = tokenSet(left);
  const b = tokenSet(right);
  if (a.size === 0 || b.size === 0) return false;
  let common = 0;
  for (const token of a) {
    if (b.has(token)) common += 1;
  }
  const union = a.size + b.size - common;
  const jaccard = union ? common / union : 0;
  const containment = common / Math.min(a.size, b.size);
  return common >= 5 && (jaccard >= 0.75 || containment >= 0.85);
}

function overlappingLocation(left, right) {
  const leftPath = normalizePath(left);
  const rightPath = normalizePath(right);
  if (!leftPath || !rightPath || leftPath !== rightPath) return false;
  const a = lineRange(left);
  const b = lineRange(right);
  if (a.start === null || a.end === null || b.start === null || b.end === null) return false;
  return a.start <= b.end && b.start <= a.end;
}

function sameLogicalDefect(reviewFinding, improveSuggestion) {
  return (
    overlappingLocation(reviewFinding, improveSuggestion) &&
    equivalentProblem(
      normalizeProblem(reviewFinding, 'review'),
      normalizeProblem(improveSuggestion, 'improve')
    )
  );
}

function logicalDescriptor(item) {
  const source = item && item.source === 'review' ? 'review' : 'improve';
  const payload = source === 'review' ? item.finding : item.suggestion;
  const range = lineRange(payload || {});
  const problem = normalizeProblem(payload || {}, source);
  const descriptor = {
    path: normalizePath(payload || {}),
    start: range.start,
    end: range.end,
    problem,
  };
  if (!descriptor.path || !descriptor.problem) {
    descriptor.fallback = stable(payload || {});
  }
  return descriptor;
}

function logicalFingerprint(items) {
  const canonical = Array.from(items || [])
    .map((item) => JSON.stringify(stable(logicalDescriptor(item))))
    .sort();
  return crypto.createHash('sha256').update(JSON.stringify(canonical)).digest('hex');
}

function buildRepairBatch(reviewPayload, improveJsonl, threshold = IMPROVE_REPAIR_THRESHOLD) {
  const review = reviewPayload && reviewPayload.review ? reviewPayload.review : reviewPayload;
  if (!review || typeof review !== 'object' || !Array.isArray(review.key_issues_to_review)) {
    throw new Error('PR-Agent review JSON has no key_issues_to_review list.');
  }
  const keyIssues = review.key_issues_to_review;
  const qualifying = qualifyingImproveSuggestions(improveJsonl, threshold);
  const items = keyIssues.map((finding, index) => ({
    source: 'review',
    index,
    finding,
  }));
  let deduplicatedSuggestions = 0;
  qualifying.forEach((suggestion, index) => {
    const duplicate = keyIssues.some((finding) => sameLogicalDefect(finding, suggestion));
    if (duplicate) {
      deduplicatedSuggestions += 1;
      return;
    }
    items.push({ source: 'improve', index, suggestion });
  });
  return {
    items,
    fingerprint: logicalFingerprint(items),
    reviewCount: keyIssues.length,
    qualifyingSuggestionCount: qualifying.length,
    deduplicatedSuggestions,
    truncated: keyIssues.length === 6,
  };
}

const REVIEW_MERGE_SAFE = 'safe_to_merge';
const REVIEW_MAX_FINDINGS = 6;

const BLOCKING_SECURITY_SIGNAL_KEYS = [
  'security_concerns',
  'security_issues',
  'security_vulnerabilities',
  'critical_security_issues',
];

// Upstream clean reviews commonly report security fields as negation prose
// ("No", "None", "N/A", "No security concerns found"). Such prose must not
// block the clean-PR fast path; anything else fails closed and blocks.
const CLEAN_SECURITY_TEXTS = new Set([
  'no',
  'none',
  'n/a',
  'na',
  'nil',
  'null',
  'nope',
  '0',
  'false',
  'ok',
  'clear',
  'clean',
  'pass',
  'passed',
  'not applicable',
  'none found',
  'no findings',
  'no finding',
  'no issues',
  'no issue',
  'no errors',
  'no error',
  'no tool errors',
  'no tool error',
  'no concerns',
  'no concern',
  'no risks',
  'no risk',
  'no vulnerabilities',
  'no vulnerability',
  'no problems',
  'no problem',
  'no threats',
  'no threat',
  'no security concerns',
  'no security issues',
  'no security vulnerabilities',
  'no critical issues',
  'no critical security issues',
]);

const CLEAN_SECURITY_SUFFIXES = [' found', ' detected', ' identified', ' observed'];

function normalizeSecurityText(value) {
  const norm = String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9/ ]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return norm;
}

function isCleanSecurityText(value) {
  const norm = normalizeSecurityText(value);
  if (!norm) return true;
  if (CLEAN_SECURITY_TEXTS.has(norm)) return true;
  for (const suffix of CLEAN_SECURITY_SUFFIXES) {
    if (norm.endsWith(suffix) && CLEAN_SECURITY_TEXTS.has(norm.slice(0, -suffix.length).trim())) {
      return true;
    }
  }
  return false;
}

function securityValueIsBlocking(value) {
  if (value === undefined || value === null) return false;
  if (typeof value === 'string') return !isCleanSecurityText(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return false;
    return value.some(securityValueIsBlocking);
  }
  if (typeof value === 'object') {
    const keys = Object.keys(value);
    if (keys.length === 0) return false;
    return keys.some((key) => securityValueIsBlocking(value[key]));
  }
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  return Boolean(value);
}

const TOOL_ERROR_SIGNAL_KEYS = [
  'tool_errors',
  'tool_error',
  'tool_failures',
  'failed_tools',
  'errors',
  'error',
];

const COVERAGE_FLAG_KEYS = [
  'review_coverage_complete',
  'coverage_complete',
  'coverage_completed',
  'is_complete',
  'is_completed',
  'complete',
  'truncated',
  'partial',
  'incomplete',
];

const COVERAGE_OBJECT_KEYS = [
  'coverage',
  'review_coverage',
  'chunk_coverage',
  'review_coverage_footer',
  'coverage_footer',
];

function hasToolErrorSignal(reviewPayload) {
  const review = unwrapReview(reviewPayload);
  for (const key of TOOL_ERROR_SIGNAL_KEYS) {
    if (!(key in review)) continue;
    if (securityValueIsBlocking(review[key])) return true;
  }
  return false;
}

function coverageFlagValueIsIncomplete(key, value) {
  if (key === 'truncated' || key === 'partial' || key === 'incomplete') {
    if (value === true) return true;
    if (typeof value === 'boolean') return false;
    if (typeof value === 'string' && ['1', 'true', 'yes'].includes(value.trim().toLowerCase())) {
      return true;
    }
    if (typeof value === 'number' && value !== 0) return true;
    return false;
  }
  if (value === false) return true;
  if (typeof value === 'boolean') return false;
  if (typeof value === 'string' && ['0', 'false', 'no'].includes(value.trim().toLowerCase())) {
    return true;
  }
  if (typeof value === 'number') {
    if (value === 0) return true;
    if (Number.isNaN(value)) return true; // fail closed
  }
  return false;
}

function hasIncompleteCoverageSignal(reviewPayload) {
  const review = unwrapReview(reviewPayload);
  for (const key of COVERAGE_FLAG_KEYS) {
    if (!(key in review)) continue;
    const value = review[key];
    if (coverageFlagValueIsIncomplete(key, value)) return true;
  }
  for (const key of COVERAGE_OBJECT_KEYS) {
    if (!(key in review)) continue;
    const value = review[key];
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const reviewed = value.reviewed !== undefined ? value.reviewed : value.reviewed_chunks;
      const total = value.total !== undefined ? value.total : value.total_chunks;
      const reviewedNum = reviewed === undefined || reviewed === null || reviewed === '' ? null : Number(reviewed);
      const totalNum = total === undefined || total === null || total === '' ? null : Number(total);
      if (reviewedNum !== null && totalNum !== null && Number.isFinite(reviewedNum) && Number.isFinite(totalNum)) {
        if (totalNum <= 0 || reviewedNum < totalNum) return true;
      }
      // Flags are independent of counts: a full count never masks an
      // explicit incomplete flag (fail closed).
      for (const flagKey of COVERAGE_FLAG_KEYS) {
        if (flagKey in value && coverageFlagValueIsIncomplete(flagKey, value[flagKey])) {
          return true;
        }
      }
      continue;
    }
    if (typeof value === 'string') {
      const lowered = value.trim().toLowerCase();
      if (!lowered) continue;
      const mentionsGap =
        lowered.includes('partial') || lowered.includes('incomplete') || lowered.includes('truncated');
      const claimsComplete = lowered.replace(/incomplete/g, '').includes('complete');
      if (mentionsGap && !claimsComplete) return true;
    }
  }
  return false;
}

function unwrapReview(reviewPayload) {
  if (
    reviewPayload &&
    typeof reviewPayload === 'object' &&
    !Array.isArray(reviewPayload) &&
    reviewPayload.review &&
    typeof reviewPayload.review === 'object' &&
    !Array.isArray(reviewPayload.review)
  ) {
    return reviewPayload.review;
  }
  if (!reviewPayload || typeof reviewPayload !== 'object' || Array.isArray(reviewPayload)) {
    throw new Error('PR-Agent review JSON must be an object.');
  }
  return reviewPayload;
}

function hasBlockingSecuritySignal(reviewPayload) {
  const review = unwrapReview(reviewPayload);
  for (const key of BLOCKING_SECURITY_SIGNAL_KEYS) {
    const value = review[key];
    if (value === undefined || value === null) continue;
    if (securityValueIsBlocking(value)) return true;
  }
  return false;
}

function persistentHasActive(persistentState) {
  if (!persistentState || typeof persistentState !== 'object' || Array.isArray(persistentState)) {
    throw new Error('Upstream finding state must be the v0.46.0 state object.');
  }
  const findings = persistentState.findings;
  if (!Array.isArray(findings)) {
    throw new Error('Upstream finding state findings must be a list.');
  }
  for (const entry of findings) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      throw new Error('Upstream finding state contains a non-object finding.');
    }
    const state = String(entry.state || '').trim().toUpperCase();
    if (state !== 'ACTIVE' && state !== 'RESOLVED') {
      throw new Error('Upstream finding state contains an unknown state.');
    }
    if (state === 'ACTIVE') return true;
  }
  return false;
}

function isCleanReviewForImproveSkip(reviewPayload, persistentState, options = {}) {
  const opts = options && typeof options === 'object' ? options : {};
  const headMatches = opts.headMatches === true;
  const review = unwrapReview(reviewPayload);
  const toolError = opts.toolError === true || hasToolErrorSignal(review);
  const reviewCoverageComplete =
    opts.reviewCoverageComplete !== false && !hasIncompleteCoverageSignal(review);
  if (toolError) {
    return { skip: false, reason: 'tool error: failing closed' };
  }
  if (!reviewCoverageComplete) {
    return { skip: false, reason: 'incomplete review coverage: failing closed' };
  }
  if (!headMatches) {
    return { skip: false, reason: 'stale head: result is not for the current HEAD' };
  }
  const keyIssues = review.key_issues_to_review;
  if (!Array.isArray(keyIssues)) {
    throw new Error('PR-Agent review JSON has no key_issues_to_review list.');
  }
  const recommendation = String(review.merge_recommendation || '').trim();
  if (!recommendation) {
    throw new Error('PR-Agent review has no merge_recommendation.');
  }
  if (recommendation !== REVIEW_MERGE_SAFE) {
    return { skip: false, reason: `merge recommendation blocks: ${recommendation}` };
  }
  if (keyIssues.length > 0) {
    if (keyIssues.length === REVIEW_MAX_FINDINGS) {
      return { skip: false, reason: 'review batch reached the findings cap: potentially truncated' };
    }
    return { skip: false, reason: `${keyIssues.length} current key issue(s) remain` };
  }
  if (hasBlockingSecuritySignal(review)) {
    return { skip: false, reason: 'blocking security signal remains' };
  }
  if (!persistentState || typeof persistentState !== 'object' || Array.isArray(persistentState)) {
    throw new Error('Upstream finding state must be the v0.46.0 state object.');
  }
  if (!Array.isArray(persistentState.findings)) {
    throw new Error('Upstream finding state findings must be a list.');
  }
  const lastRun = persistentState.last_run;
  if (!lastRun || typeof lastRun !== 'object') {
    throw new Error('Upstream PR-Agent persistent state has no last_run.');
  }
  if (lastRun.complete !== true || String(lastRun.kind || '') !== 'full') {
    throw new Error('Persistent state does not represent a complete full review.');
  }
  const stateHead = String(lastRun.head_sha || lastRun.headSha || '').trim().toLowerCase();
  if (!stateHead) {
    throw new Error('Upstream PR-Agent persistent state has no last_run.head_sha.');
  }
  const reviewedHeadSha = String(
    opts.reviewedHeadSha || opts.reviewed_head_sha || opts.headSha || opts.head_sha || ''
  ).trim().toLowerCase();
  if (!reviewedHeadSha) {
    throw new Error('Cannot decide improve skip without the exact reviewed HEAD.');
  }
  if (stateHead !== reviewedHeadSha) {
    return { skip: false, reason: 'stale persistent state: not for the reviewed HEAD' };
  }
  if (persistentHasActive(persistentState)) {
    return { skip: false, reason: 'native persistent state has an ACTIVE finding' };
  }
  return {
    skip: true,
    reason: 'clean exact HEAD: safe_to_merge with zero findings and complete state; automatic improve skipped',
  };
}

function isSkippedCleanImprovePayload(raw) {
  // Whether the improve payload carries the `improve_skipped` step's clean
  // marker. The skipped step records an empty suggestion payload marked
  // with `continuum.improve_skipped_clean` so downstream repair/merge gating
  // can tell "improve skipped for a clean HEAD" apart from "improve never
  // ran": only the former satisfies the improve-coverage leg with an empty
  // payload (mirrors GateInputs.improve_skipped_clean). Extra keys never
  // affect suggestion parsing. Unparseable lines are not clean.
  for (const line of String(raw || '').split('\n')) {
    if (!line.trim()) continue;
    let record;
    try {
      record = JSON.parse(line);
    } catch (err) {
      return false;
    }
    if (record && typeof record === 'object' && !Array.isArray(record)) {
      const marker = record.continuum;
      if (
        marker && typeof marker === 'object' && !Array.isArray(marker) &&
        marker.improve_skipped_clean === true
      ) {
        return true;
      }
    }
  }
  return false;
}

function controllerStateBody(stateMarker, summary) {
  return [
    CONTROLLER_STATE_MARKER,
    String(stateMarker || '').trim(),
    '<details>',
    '<summary>Continuum PR-Agent controller state</summary>',
    '',
    String(summary || '').trim(),
    '',
    '</details>',
  ].join('\n');
}

module.exports = {
  BLOCKING_SECURITY_SIGNAL_KEYS,
  CONTROLLER_STATE_MARKER,
  IMPROVE_REPAIR_THRESHOLD,
  REVIEW_MAX_FINDINGS,
  REVIEW_MERGE_SAFE,
  buildRepairBatch,
  controllerStateBody,
  hasBlockingSecuritySignal,
  hasIncompleteCoverageSignal,
  hasToolErrorSignal,
  isCleanReviewForImproveSkip,
  isSkippedCleanImprovePayload,
  logicalFingerprint,
  normalizeProblem,
  overlappingLocation,
  parseImproveJsonl,
  persistentHasActive,
  qualifyingImproveSuggestions,
  sameLogicalDefect,
  unwrapReview,
};
