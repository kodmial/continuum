'use strict';

const crypto = require('crypto');

const IMPROVE_REPAIR_THRESHOLD = 7;
const CONTROLLER_STATE_MARKER = '<!-- continuum-pr-agent-controller-state:v1 -->';
const TICKET_COMPLIANCE_KEY = 'ticket_compliance_check';

// Continuum comment attribution (kodmial/continuum#258). Every
// human-visible comment created or updated by Continuum must make machine
// authorship unambiguous even when the GitHub actor renders as the
// repository owner. The canonical contract lives in
// src/continuum/comment_attribution.py; these JS mirrors must stay
// byte-identical to it. Attribution is inserted into the existing body --
// a second "signature" comment is never posted and single-comment
// coalescing semantics are unchanged.
const COMMENT_ATTRIBUTION_CONTINUUM_PREFIX = '⚡ **Continuum · ';
const COMMENT_ATTRIBUTION_AGENT_CODER_PREFIX = '🦾 **Agent Coder · ';
const COMMENT_ATTRIBUTION_PROJECT_PREFIX = '🛰️ **Project · ';

function attributionHeader(role, component) {
  if (role === 'continuum') {
    return COMMENT_ATTRIBUTION_CONTINUUM_PREFIX + component + '**';
  }
  if (role === 'agent-coder') {
    return COMMENT_ATTRIBUTION_AGENT_CODER_PREFIX + component + '**';
  }
  throw new Error('unknown attribution role: ' + String(role));
}

function originMarker(role, component) {
  return '<!-- continuum-origin role=' + role + ' component=' + component + ' -->';
}

// The shared controllerStateBody serves the review controller
// (kind=review) and the repair controller (kind=repair, no-progress
// markers). The component is derived from the state marker itself so the
// long-standing two-argument call shape -- and every existing caller --
// keeps working unchanged.
function controllerAttributionComponent(stateMarker) {
  const text = String(stateMarker || '');
  const kindMatch = /\bkind=(review|repair)\b/.exec(text);
  if (kindMatch) return 'pr-agent-' + kindMatch[1];
  if (text.indexOf('continuum-pr-agent-no-progress') !== -1) return 'pr-agent-repair';
  return 'pr-agent';
}

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
  const review = unwrapReview(reviewPayload);
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
    truncated: keyIssues.length === REVIEW_MAX_FINDINGS,
  };
}

const REVIEW_MERGE_SAFE = 'safe_to_merge';
const REVIEW_MAX_FINDINGS = 6;

// Severity ranking for conflicting split-envelope merge recommendations.
// Higher wins so the merged view never understates severity:
// changes_required > merge_with_caution > safe_to_merge, with unknown
// non-empty prose failing closed as most restrictive.
function mergeRecommendationSeverity(text) {
  const normalized = String(text || '').trim();
  if (normalized === 'safe_to_merge') return 0;
  if (normalized === 'merge_with_caution') return 1;
  if (normalized === 'changes_required') return 2;
  return 3;
}

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
];

// Generic keys only signal a tool error when their content explicitly
// describes a tool failure (e.g. "upstream tool failed"). Ordinary
// code-error summaries (e.g. "2 lint errors noted in diff") must still
// skip when otherwise clean.
const GENERIC_TOOL_ERROR_KEYS = ['errors', 'error'];

// Tool words and failure words marking explicit tool-failure prose inside
// a generic `errors`/`error` value (whole-word, case-insensitive).
// Negation/empty prose is already excluded by securityValueIsBlocking
// before this check runs. A generic summary counts as a tool failure only
// when it names the failing infrastructure (`tool`/`tools`, or an
// infrastructure noun such as `api`, `model`, `llm`, `provider`, `server`,
// or `upstream` for reports like "API timeout contacting model" that never
// use the literal word `tool`) AND reports a failure
// (`failed`, `failure`, `timeout`, `timed out`, `traceback`, `exception`,
// `unavailable`, `error`/`errors`). Requiring the conjunction keeps
// ordinary code summaries such as `errors: 2 checks failed in diff` or
// `errors: missing timeout handling` (failure word without an
// infrastructure mention)
// clean while still catching explicit prose such as "upstream tool failed"
// or "API timeout contacting model". Whole-word matching additionally
// keeps substrings such as "tooling" or "exceptional" clean.
const GENERIC_TOOL_WORDS = ['tool', 'tools', 'api', 'model', 'llm', 'provider', 'server', 'upstream'];

const GENERIC_TOOL_FAILURE_WORDS = [
  'failed',
  'failure',
  'timeout',
  'timed out',
  'traceback',
  'exception',
  'unavailable',
  'error',
  'errors',
];

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const GENERIC_TOOL_WORD_PATTERNS = GENERIC_TOOL_WORDS.map(
  (marker) => new RegExp(`\\b${escapeRegExp(marker)}\\b`, 'i')
);

const GENERIC_TOOL_FAILURE_PATTERNS = GENERIC_TOOL_FAILURE_WORDS.map(
  (marker) => new RegExp(`\\b${escapeRegExp(marker)}\\b`, 'i')
);

function isGenericToolFailureText(value) {
  if (Array.isArray(value)) {
    if (value.length === 0) return false;
    return value.some(isGenericToolFailureText);
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value);
    if (keys.length === 0) return false;
    return keys.some((key) => isGenericToolFailureText(value[key]));
  }
  if (typeof value !== 'string') {
    // Explicit non-string error evidence (e.g. `errors: true` or
    // `errors: 1`) must force improve for repair value instead of
    // authorizing a clean skip: fail closed via the blocking check.
    // Clean values (false, 0, null/undefined) stay clean.
    return securityValueIsBlocking(value);
  }
  if (!securityValueIsBlocking(value)) return false;
  const text = value.trim();
  if (!GENERIC_TOOL_WORD_PATTERNS.some((pattern) => pattern.test(text))) return false;
  return GENERIC_TOOL_FAILURE_PATTERNS.some((pattern) => pattern.test(text));
}

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

// Generic keys routinely carry ordinary prose (e.g.
// `"complete": "completed review summary"`), so only explicit false-like
// values on those keys signal incomplete coverage; unparseable strings
// and other shapes are ignored.
const GENERIC_COVERAGE_KEYS = new Set(['is_complete', 'is_completed', 'complete']);

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
  for (const key of GENERIC_TOOL_ERROR_KEYS) {
    if (!(key in review)) continue;
    if (isGenericToolFailureText(review[key])) return true;
  }
  return false;
}

function coverageFlagValueIsIncomplete(key, value) {
  if (key === 'truncated' || key === 'partial' || key === 'incomplete') {
    if (value === true) return true;
    if (typeof value === 'boolean') return false;
    if (value === null || value === undefined) return true;
    if (typeof value === 'string') {
      const lowered = value.trim().toLowerCase();
      // An explicit truncation key with an empty value is not evidence of
      // complete coverage: fail closed like null/undefined so
      // `{"truncated": ""}` never authorizes an improve skip.
      if (!lowered) return true;
      if (['1', 'true', 'yes'].includes(lowered)) return true;
      if (['0', 'false', 'no'].includes(lowered)) return false;
      const numeric = Number(lowered);
      if (lowered !== '' && Number.isFinite(numeric)) {
        return numeric !== 0;
      }
      // Unknown string coverage state fails closed.
      return true;
    }
    if (typeof value === 'number') {
      if (Number.isNaN(value)) return true;
      if (!Number.isFinite(value)) return true;
      return value !== 0;
    }
    return true;
  }
  if (GENERIC_COVERAGE_KEYS.has(key)) {
    // Generic keys carry prose as often as coverage state: only explicit
    // false-like values block the clean fast path. Unparseable strings
    // and other shapes are ignored (no signal).
    if (typeof value === 'boolean') return value === false;
    if (value === null || value === undefined) return false;
    if (typeof value === 'string') {
      const lowered = value.trim().toLowerCase();
      if (!lowered) return false;
      if (['0', 'false', 'no'].includes(lowered)) return true;
      if (['1', 'true', 'yes'].includes(lowered)) return false;
      const numeric = Number(lowered);
      if (lowered !== '' && Number.isFinite(numeric)) {
        return numeric === 0;
      }
      return false;
    }
    if (typeof value === 'number') {
      if (Number.isNaN(value)) return false;
      if (!Number.isFinite(value)) return false;
      return value === 0;
    }
    return false;
  }
  if (typeof value === 'boolean') return value === false;
  if (value === null || value === undefined) return true;
  if (typeof value === 'string') {
    const lowered = value.trim().toLowerCase();
    // An explicit coverage key with an empty value is not evidence of
    // complete coverage: fail closed like null/undefined.
    if (!lowered) return true;
    if (['0', 'false', 'no'].includes(lowered)) return true;
    if (['1', 'true', 'yes'].includes(lowered)) return false;
    const numeric = Number(lowered);
    if (lowered !== '' && Number.isFinite(numeric)) {
      return numeric === 0;
    }
    // Unknown string coverage state fails closed.
    return true;
  }
  if (typeof value === 'number') {
    if (Number.isNaN(value)) return true; // fail closed
    if (!Number.isFinite(value)) return true;
    return value === 0;
  }
  return true;
}

function hasIncompleteCoverageSignal(reviewPayload) {
  const review = unwrapReview(reviewPayload);
  let seenCoverage = false;
  for (const key of COVERAGE_FLAG_KEYS) {
    if (!(key in review)) continue;
    seenCoverage = true;
    const value = review[key];
    if (coverageFlagValueIsIncomplete(key, value)) return true;
  }
  for (const key of COVERAGE_OBJECT_KEYS) {
    if (!(key in review)) continue;
    seenCoverage = true;
    const value = review[key];
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const reviewed = value.reviewed !== undefined ? value.reviewed : value.reviewed_chunks;
      const total = value.total !== undefined ? value.total : value.total_chunks;
      const hasReviewedKey = value.reviewed !== undefined || value.reviewed_chunks !== undefined;
      const hasTotalKey = value.total !== undefined || value.total_chunks !== undefined;
      const reviewedNum = reviewed === undefined || reviewed === null || reviewed === '' ? null : Number(reviewed);
      const totalNum = total === undefined || total === null || total === '' ? null : Number(total);
      const hasCountSignal = hasReviewedKey || hasTotalKey;
      const hasFlagSignal = COVERAGE_FLAG_KEYS.some((flagKey) => flagKey in value);
      if (hasCountSignal) {
        // Fail closed on unparseable counts: a present-but-unrecognized
        // count never reads as complete coverage.
        if (reviewedNum === null || totalNum === null || !Number.isFinite(reviewedNum) || !Number.isFinite(totalNum)) {
          return true;
        }
        if (totalNum <= 0 || reviewedNum < totalNum) return true;
      }
      // Flags are independent of counts: a full count never masks an
      // explicit incomplete flag (fail closed).
      for (const flagKey of COVERAGE_FLAG_KEYS) {
        if (flagKey in value && coverageFlagValueIsIncomplete(flagKey, value[flagKey])) {
          return true;
        }
      }
      // A coverage object with no recognized counts or flags (e.g. {} or
      // only unknown fields) is not evidence of complete coverage: fail
      // closed so unknown coverage never permits an improve skip.
      if (!hasCountSignal && !hasFlagSignal) return true;
      continue;
    }
    if (typeof value === 'string') {
      // A coverage-object key expects a mapping with counts/flags; any
      // string value is an unrecognized shape: fail closed so unknown
      // coverage never permits an improve skip.
      return true;
    }
    // Any other non-mapping value (boolean, number, null, array, ...)
    // is an unrecognized coverage shape: fail closed.
    return true;
  }
  if (!seenCoverage) {
    // Absent coverage keys carry no incomplete signal: a clean payload
    // without coverage footers (e.g. upstream safe_to_merge reviews that
    // omit `coverage`/`coverage_complete`) still reaches the skip path when
    // the caller explicitly passes `reviewCoverageComplete: true` (an
    // omitted flag still fails closed via isCleanReviewForImproveSkip).
    // Unknown or unparseable present coverage shapes still fail closed.
    // Mirrors Python has_incomplete_coverage_signal.
    return false;
  }
  return false;
}

function coverageSingleValueIsIncomplete(key, value) {
  if (COVERAGE_FLAG_KEYS.includes(key)) {
    return coverageFlagValueIsIncomplete(key, value);
  }
  if (COVERAGE_OBJECT_KEYS.includes(key)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const reviewed = value.reviewed !== undefined ? value.reviewed : value.reviewed_chunks;
      const total = value.total !== undefined ? value.total : value.total_chunks;
      const hasReviewedKey = value.reviewed !== undefined || value.reviewed_chunks !== undefined;
      const hasTotalKey = value.total !== undefined || value.total_chunks !== undefined;
      const reviewedNum = reviewed === undefined || reviewed === null || reviewed === '' ? null : Number(reviewed);
      const totalNum = total === undefined || total === null || total === '' ? null : Number(total);
      const hasCountSignal = hasReviewedKey || hasTotalKey;
      const hasFlagSignal = COVERAGE_FLAG_KEYS.some((flagKey) => flagKey in value);
      if (hasCountSignal) {
        if (reviewedNum === null || totalNum === null || !Number.isFinite(reviewedNum) || !Number.isFinite(totalNum)) {
          return true;
        }
        if (totalNum <= 0 || reviewedNum < totalNum) return true;
      }
      for (const flagKey of COVERAGE_FLAG_KEYS) {
        if (flagKey in value && coverageFlagValueIsIncomplete(flagKey, value[flagKey])) {
          return true;
        }
      }
      if (!hasCountSignal && !hasFlagSignal) return true;
      return false;
    }
    if (typeof value === 'string') {
      // A coverage-object key expects a mapping with counts/flags; any
      // string value is an unrecognized shape: fail closed.
      return true;
    }
    // Any other non-mapping value (boolean, number, null, array, ...)
    // is an unrecognized coverage shape: fail closed.
    return true;
  }
  return false;
}

function unwrapReview(reviewPayload) {
  if (!reviewPayload || typeof reviewPayload !== 'object' || Array.isArray(reviewPayload)) {
    throw new Error('PR-Agent review JSON must be an object.');
  }
  const nested = reviewPayload.review;
  const nestedIsObject =
    nested && typeof nested === 'object' && !Array.isArray(nested);
  if (!nestedIsObject) {
    return reviewPayload;
  }
  const outerHasSignal =
    'key_issues_to_review' in reviewPayload ||
    'merge_recommendation' in reviewPayload ||
    TICKET_COMPLIANCE_KEY in reviewPayload ||
    BLOCKING_SECURITY_SIGNAL_KEYS.some((key) => key in reviewPayload) ||
    TOOL_ERROR_SIGNAL_KEYS.some((key) => key in reviewPayload) ||
    GENERIC_TOOL_ERROR_KEYS.some((key) => key in reviewPayload) ||
    COVERAGE_FLAG_KEYS.some((key) => key in reviewPayload) ||
    COVERAGE_OBJECT_KEYS.some((key) => key in reviewPayload);
  if (!outerHasSignal) {
    return nested;
  }
  // Split envelope: signals are divided between the outer payload and the
  // nested `review` object. Returning only one side discards the other:
  // `{coverage_complete: true, review: {key_issues...}}` would miss the
  // nested findings, and `{key_issues: [], review: {security_concerns:
  // ...}}` would miss nested blocking security. Merge both sides
  // fail-closed so no finding, recommendation, security, tool-error, or
  // coverage signal is lost.
  const merged = {};
  for (const [key, value] of Object.entries(nested)) {
    if (key === 'review') continue;
    merged[key] = value;
  }
  for (const [key, value] of Object.entries(reviewPayload)) {
    if (key === 'review') continue;
    if (!(key in merged)) {
      merged[key] = value;
      continue;
    }
    const current = merged[key];
    if (key === 'key_issues_to_review') {
      const currentIsList = Array.isArray(current);
      const outerIsList = Array.isArray(value);
      if (currentIsList && outerIsList) {
        merged[key] = [...value, ...current];
      } else if (!currentIsList) {
        // Keep the non-list so callers fail closed on invalid shape
        // instead of silently reading the clean side.
      } else {
        // Outer is non-list while nested is a list: surface the invalid
        // shape so validation throws rather than reading clean.
        merged[key] = value;
      }
      continue;
    }
    if (key === 'merge_recommendation') {
      const currentText = String(current || '').trim();
      const outerText = String(value || '').trim();
      if (!currentText) {
        merged[key] = value;
      } else if (!outerText) {
        // Keep the present recommendation.
      } else if (currentText !== outerText) {
        // Most restrictive wins so split envelopes never understate
        // severity: changes_required > merge_with_caution > safe_to_merge,
        // with unknown non-empty prose failing closed as most restrictive.
        if (mergeRecommendationSeverity(outerText) > mergeRecommendationSeverity(currentText)) {
          merged[key] = value;
        }
      }
      continue;
    }
    if (key === TICKET_COMPLIANCE_KEY) {
      // Large-PR chunking and split envelopes can carry ticket evidence on
      // both sides. Every ticket must be evaluated, so union both lists
      // instead of letting one side discard the other. Any non-list side is
      // surfaced so strict ticket validation fails closed.
      if (Array.isArray(current) && Array.isArray(value)) {
        merged[key] = [...value, ...current];
      } else if (!Array.isArray(current)) {
        // Keep the malformed nested side so strict validation fails closed.
      } else {
        // The outer side is malformed; surface it instead of the clean list.
        merged[key] = value;
      }
      continue;
    }
    if (
      BLOCKING_SECURITY_SIGNAL_KEYS.includes(key) ||
      TOOL_ERROR_SIGNAL_KEYS.includes(key)
    ) {
      // Either side blocking must block the merged view.
      if (!securityValueIsBlocking(current) && securityValueIsBlocking(value)) {
        merged[key] = value;
      }
      continue;
    }
    if (GENERIC_TOOL_ERROR_KEYS.includes(key)) {
      // Either side reporting an explicit generic tool failure must block
      // the merged view; ordinary summaries stay clean.
      if (!isGenericToolFailureText(current) && isGenericToolFailureText(value)) {
        merged[key] = value;
      }
      continue;
    }
    if (COVERAGE_FLAG_KEYS.includes(key) || COVERAGE_OBJECT_KEYS.includes(key)) {
      // Either side incomplete must read as incomplete.
      const currentIncomplete = coverageSingleValueIsIncomplete(key, current);
      const outerIncomplete = coverageSingleValueIsIncomplete(key, value);
      if (outerIncomplete && !currentIncomplete) {
        merged[key] = value;
      } else if (!currentIncomplete && !outerIncomplete) {
        if (
          current &&
          typeof current === 'object' &&
          !Array.isArray(current) &&
          value &&
          typeof value === 'object' &&
          !Array.isArray(value)
        ) {
          merged[key] = { ...current, ...value };
          // Re-check the shallow merge: if either original was incomplete
          // the branch above already kept it, so a complete+complete merge
          // stays complete unless the combination itself is incomplete.
          if (coverageSingleValueIsIncomplete(key, merged[key])) {
            // Keep the merged incomplete object (fail closed).
          }
        } else {
          merged[key] = value;
        }
      }
      // Else keep the incomplete current value (fail closed).
      continue;
    }
    merged[key] = value;
  }
  return merged;
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

function isPlausibleHeadSha(value) {
  // A HEAD value looks like a commit SHA, not a placeholder. Production
  // HEADs are 40/64 hex; abbreviated SHAs are at least 7 hex characters.
  // Identical placeholders such as "unknown" contain non-hex characters
  // and must never satisfy the exact-HEAD skip check, and trivially short
  // hex fragments (e.g. "a", "123", "abc") from a bug or mocked HEAD must
  // not authorize a skip either, so require at least short-SHA length
  // instead of mere non-emptiness (mirrors Python _is_plausible_head_sha).
  // Git additionally reserves the all-zero object id ("0000000...") for
  // "no object": matching zero placeholders on both sides must never pass
  // the exact-HEAD check even though both sides are hex of plausible
  // length. Production always compares full HEADs from the GitHub API, so
  // abbreviated prefixes never occur there; the length floor screens
  // mocked fragments while genuine abbreviations stay valid.
  const text = String(value || '').trim().toLowerCase();
  if (!/^[0-9a-f]{7,64}$/.test(text)) return false;
  if (/^0+$/.test(text)) return false;
  return true;
}

function isCleanReviewForImproveSkip(reviewPayload, persistentState, options = {}) {
  const opts = options && typeof options === 'object' ? options : {};
  const headMatches = opts.headMatches === true;
  let review;
  try {
    review = unwrapReview(reviewPayload);
  } catch (err) {
    // An upstream review-shape variation must safely run improve for
    // repair value instead of crashing the orchestrator: fail closed to
    // skip:false, never to an exception.
    return { skip: false, reason: 'invalid review payload: failing closed' };
  }
  const toolError = opts.toolError === true || hasToolErrorSignal(review);
  if (toolError) {
    return { skip: false, reason: 'tool error: failing closed' };
  }
  if (!headMatches) {
    return { skip: false, reason: 'stale head: result is not for the current HEAD' };
  }
  const keyIssues = review.key_issues_to_review;
  if (!Array.isArray(keyIssues)) {
    // A benign upstream shape variation must still run improve for repair
    // value instead of crashing the orchestrator: fail closed to
    // skip:false, never to an exception.
    return { skip: false, reason: 'review has no key_issues_to_review list: failing closed' };
  }
  const recommendation = String(review.merge_recommendation || '').trim();
  if (!recommendation) {
    return { skip: false, reason: 'review has no merge_recommendation: failing closed' };
  }
  // The exact reviewed HEAD is mandatory for any skip decision: without it
  // the orchestrator must still safely run improve for repair value instead
  // of crashing, so a missing HEAD fails closed to skip:false (never to an
  // exception) while benign variations with a known HEAD do the same.
  const reviewedHeadSha = String(
    opts.reviewedHeadSha || opts.reviewed_head_sha || opts.headSha || opts.head_sha || ''
  ).trim().toLowerCase();
  if (!reviewedHeadSha) {
    return { skip: false, reason: 'cannot decide improve skip without the exact reviewed HEAD: failing closed' };
  }
  // Fail closed on the explicit opt-in plus positive coverage evidence
  // in the review payload. An omitted flag never skips, and absent
  // coverage keys fail closed via hasIncompleteCoverageSignal (no
  // positive evidence); the findings-cap truncation check applies
  // separately.
  const reviewCoverageComplete =
    opts.reviewCoverageComplete === true && !hasIncompleteCoverageSignal(review);
  if (!reviewCoverageComplete) {
    return { skip: false, reason: 'incomplete review coverage: failing closed' };
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
    return { skip: false, reason: 'persistent state is not an object: failing closed' };
  }
  if (!Array.isArray(persistentState.findings)) {
    // A benign upstream format variation must still run improve for repair
    // value instead of crashing the orchestrator: fail closed to
    // skip:false, never to an exception.
    return { skip: false, reason: 'persistent state findings is not a list: failing closed' };
  }
  const lastRun = persistentState.last_run;
  if (!lastRun || typeof lastRun !== 'object') {
    return { skip: false, reason: 'persistent state has no last_run: failing closed' };
  }
  if (lastRun.complete !== true || String(lastRun.kind || '') !== 'full') {
    return { skip: false, reason: 'persistent state is not from a complete full review: failing closed' };
  }
  const stateHead = String(lastRun.head_sha || lastRun.headSha || '').trim().toLowerCase();
  if (!stateHead) {
    // Benign schema variation (e.g. an upstream field omission): run
    // improve for repair value instead of crashing the orchestrator.
    return { skip: false, reason: 'persistent state has no last_run.head_sha: failing closed' };
  }
  if (!isPlausibleHeadSha(stateHead) || !isPlausibleHeadSha(reviewedHeadSha)) {
    // Identical placeholders (e.g. "unknown" on both sides) must never
    // pass the exact-HEAD check: only a real commit SHA shape can allow a
    // skip. Fail closed to improve, never to an exception.
    return { skip: false, reason: 'unrecognized HEAD shape: failing closed' };
  }
  if (stateHead !== reviewedHeadSha) {
    return { skip: false, reason: 'stale persistent state: not for the reviewed HEAD' };
  }
  let hasActive;
  try {
    hasActive = persistentHasActive(persistentState);
  } catch (err) {
    // A benign upstream format variation (e.g. a new finding state) must
    // safely run improve for repair value instead of crashing the
    // orchestrator: fail closed to skip:false, never to an exception.
    return { skip: false, reason: 'persistent state has an unrecognized finding state: failing closed' };
  }
  if (hasActive) {
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
  // affect suggestion parsing. Every line must parse: any unparseable line
  // means the payload is not clean, regardless of where the marker appears.
  let foundMarker = false;
  let sawLine = false;
  for (const line of String(raw || '').split('\n')) {
    if (!line.trim()) continue;
    sawLine = true;
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
        foundMarker = true;
      }
    }
  }
  return sawLine && foundMarker;
}

// Upstream v0.46.0 documents ticket_compliance_check as a list of
// TicketCompliance objects whose not_compliant_requirements and
// requires_further_human_verification fields are free-form bullet prose
// (strings), not typed actions. Only definitively empty/negated prose is
// empty. Everything else is a positive compliance signal that no rerun of
// the same review can repair because it depends on PR/issue metadata.
const EMPTY_COMPLIANCE_TEXTS = new Set([
  '',
  '-',
  '--',
  '0',
  'false',
  'n/a',
  'na',
  'nil',
  'no',
  'none',
  'none.',
  '[]',
  '{}',
  'nothing',
  'not applicable',
  'nope',
  'null',
  'no gaps',
  'no gap',
  'no issues',
  'no issue',
  'no findings',
  'no finding',
  'no compliance issues',
  'no compliance issue',
  'no compliance concerns',
  'no compliance concern',
  'no unmet requirements',
  'no unmet requirement',
  'no outstanding requirements',
  'no outstanding requirement',
  'no outstanding issues',
  'no outstanding issue',
  'no missing requirements',
  'no non-compliance',
  'no noncompliance',
  'no non-compliant requirements',
  'no noncompliant requirements',
  'no compliance gaps',
  'no compliance gap',
  'no requirements missing',
  'no acceptance criteria gaps',
  'no acceptance criteria missing',
  'nothing to report',
  'nothing missing',
  'all requirements met',
  'all requirements are met',
  'all requirements were met',
  'all requirements have been met',
  'all requirements satisfied',
  'all requirements are satisfied',
  'all requirements were satisfied',
  'all requirements compliant',
  'all requirements are compliant',
  'all requirements addressed',
  'all requirements are addressed',
  'all acceptance criteria met',
  'all acceptance criteria are met',
  'all acceptance criteria satisfied',
  'all acceptance criteria were satisfied',
  'all acceptance criteria are satisfied',
  'acceptance criteria met',
  'acceptance criteria are met',
  'acceptance criteria satisfied',
  'acceptance criteria are satisfied',
  'every requirement met',
  'every requirement is met',
  'every requirement satisfied',
  'every requirement is satisfied',
  'fully compliant',
  'fully satisfied',
  'completely compliant',
  'completely satisfied',
  'requirements met',
  'requirements are met',
  'requirements were met',
  'requirements satisfied',
  'requirements are satisfied',
  'requirements compliant',
]);

function normalizeComplianceLine(value) {
  let line = String(value || '').trim().toLowerCase();
  line = line.replace(/^(?:[-*+•·–—]+|\d+[.)])\s*/, '');
  line = line.replace(/^[`"'“”‘’]+|[`"'“”‘’]+$/g, '');
  line = line.replace(/\s+/g, ' ').trim();
  line = line.replace(/[.;:,!]+$/g, '').trim();
  return line;
}

function isDefinitivelyEmptyComplianceText(value) {
  // Only string fields are valid per the upstream schema; non-strings are
  // malformed and are rejected by ticketComplianceSummary before this
  // helper is called.
  if (typeof value !== 'string') return false;
  const lines = value
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map(normalizeComplianceLine)
    .filter(Boolean);
  if (lines.length === 0) return true;
  const suffixes = [
    ' found',
    ' was found',
    ' were found',
    ' identified',
    ' was identified',
    ' were identified',
    ' detected',
    ' was detected',
    ' were detected',
    ' observed',
    ' was observed',
    ' were observed',
    ' reported',
    ' was reported',
    ' were reported',
    ' remain',
    ' remains',
    ' remaining',
  ];
  return lines.every((line) => {
    if (EMPTY_COMPLIANCE_TEXTS.has(line)) return true;
    return suffixes.some((suffix) => {
      return line.endsWith(suffix) &&
        EMPTY_COMPLIANCE_TEXTS.has(line.slice(0, -suffix.length).trim());
    });
  });
}

function sanitizeComplianceText(value, limit = 320) {
  // Sanitized hold evidence: single-line, control characters removed,
  // URLs and obvious credential shapes redacted, then truncated. The exact
  // native ticket URL is carried separately in ticket_url.
  return String(value || '')
    .replace(/\r\n?/g, ' ')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]+/g, ' ')
    .replace(/https?:\/\/\S+/gi, '[url]')
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]+|rnd_[A-Za-z0-9]+)\b/g, '[redacted]')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, limit);
}

function safeTicketReference(value) {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value !== 'string') {
    throw new Error('PR-Agent ticket_compliance_check ticket_url must be a string.');
  }
  const text = value.trim();
  if (!text) return '';
  // Upstream v0.46.0 documents ticket_url as "Ticket URL or ID". Accept both
  // the exact http(s) URL and common compact IDs, but keep the value safe to
  // embed in a controller comment (no markdown/control/whitespace injection).
  const isUrl = /^https?:\/\/[A-Za-z0-9._~:/?#@$&'*+,;=%-]+$/i.test(text);
  const isId =
    /^(?:#?\d+|[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9_.-]+#\d+|[A-Za-z][A-Za-z0-9_.-]*#\d+|[A-Za-z][A-Za-z0-9_.-]*-\d+)$/.test(text);
  if (!isUrl && !isId) {
    throw new Error(
      'PR-Agent ticket_compliance_check ticket_url must be an http(s) URL or ticket ID.'
    );
  }
  return text;
}

function complianceStringField(entry, key) {
  const value = entry[key];
  if (value === undefined) {
    throw new Error('PR-Agent ticket_compliance_check entry has no ' + key + ' string.');
  }
  if (typeof value !== 'string') {
    throw new Error('PR-Agent ticket_compliance_check ' + key + ' must be a string.');
  }
  return value;
}

function optionalComplianceStringField(entry, key) {
  const value = entry[key];
  if (value === undefined || value === null) return '';
  if (typeof value !== 'string') {
    throw new Error('PR-Agent ticket_compliance_check ' + key + ' must be a string.');
  }
  return value;
}

function ticketComplianceSummary(reviewPayload) {
  // Canonical strict parser for the documented v0.46.0 structure. It
  // evaluates every entry (multiple tickets and split-envelope/chunk
  // unions) instead of inspecting only the first ticket. Missing ticket
  // context is legitimate for an issue-less PR; a present malformed
  // structure fails closed.
  const review = unwrapReview(reviewPayload);
  const raw = review[TICKET_COMPLIANCE_KEY];
  if (raw === undefined || raw === null) {
    return {
      present: false,
      ticketCount: 0,
      nonCompliant: [],
      requiresHumanVerification: [],
      fingerprint: crypto.createHash('sha256').update('absent').digest('hex'),
    };
  }
  if (!Array.isArray(raw)) {
    throw new Error('PR-Agent review ticket_compliance_check must be a list.');
  }
  const nonCompliant = [];
  const requiresHumanVerification = [];
  const canonical = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      throw new Error('PR-Agent review ticket_compliance_check entries must be objects.');
    }
    const notCompliantText = complianceStringField(entry, 'not_compliant_requirements');
    const furtherVerificationText = optionalComplianceStringField(
      entry,
      'requires_further_human_verification'
    );
    const ticketUrl = safeTicketReference(entry.ticket_url);
    // Validate the remaining documented string fields when present so a
    // contradictory non-string shape never reaches a merge decision.
    optionalComplianceStringField(entry, 'ticket_requirements');
    optionalComplianceStringField(entry, 'fully_compliant_requirements');
    const notCompliantEmpty = isDefinitivelyEmptyComplianceText(notCompliantText);
    const furtherVerificationEmpty =
      !furtherVerificationText ||
      isDefinitivelyEmptyComplianceText(furtherVerificationText);
    if (!notCompliantEmpty) {
      nonCompliant.push({
        ticket_url: ticketUrl,
        reason: sanitizeComplianceText(notCompliantText),
      });
    }
    if (!furtherVerificationEmpty) {
      requiresHumanVerification.push({
        ticket_url: ticketUrl,
        note: sanitizeComplianceText(furtherVerificationText),
      });
    }
    canonical.push({
      ticket_url: ticketUrl,
      not_compliant_requirements: notCompliantEmpty
        ? ''
        : sanitizeComplianceText(notCompliantText),
      requires_further_human_verification: furtherVerificationEmpty
        ? ''
        : sanitizeComplianceText(furtherVerificationText),
    });
  }
  const fingerprint = crypto
    .createHash('sha256')
    .update(JSON.stringify(stable(canonical)))
    .digest('hex');
  return {
    present: true,
    ticketCount: raw.length,
    nonCompliant,
    requiresHumanVerification,
    fingerprint,
  };
}

function nonActionableCautionIsMergeable(review) {
  // Upstream can legitimately choose merge_with_caution for risk/advisory
  // prose even when its exact-HEAD review found no actionable issues.
  // Re-reviewing an unchanged clean HEAD cannot fix that recommendation.
  // Require an explicit clean security signal, and reject *all* known
  // structural failure signals before treating such caution as non-actionable.
  // Reuse the canonical clean-security parser: upstream writes natural-language
  // negations ("No security concerns identified"), not just "no"/"none".
  const security = review && review.security_concerns;
  if (security === undefined || security === null || security === '') return false;
  if (hasBlockingSecuritySignal(review) ||
      hasToolErrorSignal(review) ||
      hasIncompleteCoverageSignal(review)) return false;

  // Missing/empty ticket context is not positive non-compliance. A present
  // malformed structure or any positive not_compliant_requirements string
  // keeps the merge gate closed; requires_further_human_verification alone
  // is advisory and is deliberately not equated with non-compliance.
  let ticket;
  try {
    ticket = ticketComplianceSummary(review);
  } catch (err) {
    return false;
  }
  return ticket.nonCompliant.length === 0;
}

function holdFingerprint(parts) {
  return crypto.createHash('sha256').update(JSON.stringify(stable(parts))).digest('hex');
}

function reviewDisposition(reviewPayload, improveJsonl, threshold = IMPROVE_REPAIR_THRESHOLD) {
  // Split envelopes carry signals on both sides: merge fail-closed via the
  // centralized unwrapReview so no finding is lost from the disposition.
  const review = unwrapReview(reviewPayload);
  if (!review || typeof review !== 'object' || Array.isArray(review)) {
    throw new Error('PR-Agent review payload must be an object.');
  }
  if (!Array.isArray(review.key_issues_to_review)) {
    throw new Error('PR-Agent review JSON has no key_issues_to_review list.');
  }
  const recommendation = String(review.merge_recommendation || '').trim();
  if (!['safe_to_merge', 'merge_with_caution', 'changes_required'].includes(recommendation)) {
    throw new Error(
      'PR-Agent review has invalid merge_recommendation: ' + (recommendation || '<empty>')
    );
  }
  // Strict ticket parsing happens before any routing so a malformed or
  // contradictory native structure always fails closed, even when code
  // findings exist. This is the same helper the merge gate consumes via the
  // canonical disposition below.
  const ticket = ticketComplianceSummary(review);
  const qualifying = qualifyingImproveSuggestions(improveJsonl, threshold);
  const reviewCount = review.key_issues_to_review.length;
  const common = {
    recommendation,
    reviewCount,
    qualifyingSuggestionCount: qualifying.length,
    ticketCount: ticket.ticketCount,
  };
  // The authoritative review is the merge/repair gate. /improve is an
  // auxiliary repair input only after that review has already found an
  // actionable defect. A clean reviewed HEAD must never be held in an endless
  // polish loop by a fresh suggestion discovered by the optional second pass.
  if (reviewCount > 0) {
    return {
      ...common,
      action: 'repair',
      reason:
        reviewCount + ' authoritative review finding(s), ' +
        qualifying.length + ' auxiliary improve suggestion(s)',
    };
  }
  // Independent blocking signals keep their existing fail-closed gate. They
  // must never be relabeled as a deterministic non-code hold, because the
  // existing tool/security/coverage machinery owns that classification.
  const independentBlocking =
    hasBlockingSecuritySignal(review) ||
    hasToolErrorSignal(review) ||
    hasIncompleteCoverageSignal(review);

  if (!independentBlocking) {
    if (ticket.nonCompliant.length > 0) {
      // A positive not_compliant_requirements STRING always prevents merge,
      // regardless of the model's merge_recommendation. Rerunning the same
      // reviewer on unchanged HEAD/issue/PR metadata cannot add the missing
      // external evidence, so this is a non-code hold, never repair or an
      // unchanged same-state re-review.
      const ticketUrls = ticket.nonCompliant
        .map((entry) => entry.ticket_url)
        .filter(Boolean);
      return {
        ...common,
        action: 'hold',
        hold: {
          kind: 'ticket_compliance',
          recommendation,
          ticket_urls: Array.from(new Set(ticketUrls)),
          reasons: ticket.nonCompliant.map((entry) => ({
            ticket_url: entry.ticket_url,
            reason: entry.reason,
          })),
          requires_human_verification: ticket.requiresHumanVerification,
          fingerprint: holdFingerprint({
            action: 'hold',
            kind: 'ticket_compliance',
            recommendation,
            tickets: ticket.nonCompliant,
          }),
        },
        reason:
          'non-code task-compliance hold: ' + ticket.nonCompliant.length +
          ' ticket requirement gap(s); no code repair dispatched',
      };
    }
    if (recommendation === 'changes_required') {
      // changes_required with no actionable native key issues and no ticket
      // compliance gap cannot be fixed by repeating the same-HEAD review. Hold
      // it explicitly and require changed eligible evidence/manual triage.
      return {
        ...common,
        action: 'hold',
        hold: {
          kind: 'unresolved_review',
          recommendation,
          ticket_urls: [],
          reasons: [],
          requires_human_verification: ticket.requiresHumanVerification,
          fingerprint: holdFingerprint({
            action: 'hold',
            kind: 'unresolved_review',
            recommendation,
            ticket: ticket.fingerprint,
          }),
        },
        reason:
          'non-code unresolved hold: changes_required with zero actionable code findings; ' +
          'fresh review requires changed eligible evidence or manual triage',
      };
    }
  }

  if (
    recommendation === 'safe_to_merge' ||
    (recommendation === 'merge_with_caution' && nonActionableCautionIsMergeable(review))
  ) {
    const advisory =
      ticket.requiresHumanVerification.length > 0
        ? '; advisory human-verification note(s) recorded without a new approval gate'
        : '';
    return {
      ...common,
      action: 'merge',
      reason:
        (recommendation === 'safe_to_merge'
          ? 'safe_to_merge with no actionable review/improve items'
          : 'non-actionable caution with explicit no-security/no-noncompliance evidence') +
        advisory,
    };
  }
  return {
    ...common,
    action: 'rereview',
    reason: 'blocking merge recommendation without actionable payload: ' + recommendation,
  };
}

function controllerStateBody(stateMarker, summary) {
  // Layout: the legacy hidden marker lines stay first so positional
  // upsert/election matching is unchanged (HTML comments do not render,
  // so the attribution header is still the first rendered line).
  const component = controllerAttributionComponent(stateMarker);
  return [
    CONTROLLER_STATE_MARKER,
    String(stateMarker || '').trim(),
    attributionHeader('continuum', component),
    originMarker('continuum', component),
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
  COMMENT_ATTRIBUTION_AGENT_CODER_PREFIX,
  COMMENT_ATTRIBUTION_CONTINUUM_PREFIX,
  COMMENT_ATTRIBUTION_PROJECT_PREFIX,
  CONTROLLER_STATE_MARKER,
  IMPROVE_REPAIR_THRESHOLD,
  REVIEW_MAX_FINDINGS,
  REVIEW_MERGE_SAFE,
  attributionHeader,
  buildRepairBatch,
  controllerAttributionComponent,
  controllerStateBody,
  hasBlockingSecuritySignal,
  hasIncompleteCoverageSignal,
  hasToolErrorSignal,
  isCleanReviewForImproveSkip,
  isSkippedCleanImprovePayload,
  logicalFingerprint,
  normalizeProblem,
  originMarker,
  overlappingLocation,
  parseImproveJsonl,
  persistentHasActive,
  qualifyingImproveSuggestions,
  reviewDisposition,
  nonActionableCautionIsMergeable,
  sameLogicalDefect,
  ticketComplianceSummary,
  isDefinitivelyEmptyComplianceText,
  unwrapReview,
};
