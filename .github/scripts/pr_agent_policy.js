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
      const lowered = value.trim().toLowerCase();
      if (!lowered) return false;
      const mentionsGap =
        lowered.includes('partial') || lowered.includes('incomplete') || lowered.includes('truncated');
      const claimsComplete = lowered.replace(/incomplete/g, '').includes('complete');
      return mentionsGap && !claimsComplete;
    }
    return false;
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
    BLOCKING_SECURITY_SIGNAL_KEYS.some((key) => key in reviewPayload) ||
    TOOL_ERROR_SIGNAL_KEYS.some((key) => key in reviewPayload) ||
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
        // Most restrictive wins: any non-safe recommendation blocks.
        if (currentText === REVIEW_MERGE_SAFE && outerText !== REVIEW_MERGE_SAFE) {
          merged[key] = value;
        }
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
  // The exact reviewed HEAD is mandatory for any skip decision: validate it
  // before interpreting persistent format variations so a missing HEAD
  // still fails closed by exception while a benign upstream format
  // variation with a known HEAD safely runs improve (skip:false).
  const reviewedHeadSha = String(
    opts.reviewedHeadSha || opts.reviewed_head_sha || opts.headSha || opts.head_sha || ''
  ).trim().toLowerCase();
  if (!reviewedHeadSha) {
    throw new Error('Cannot decide improve skip without the exact reviewed HEAD.');
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
    throw new Error('Upstream PR-Agent persistent state has no last_run.head_sha.');
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
