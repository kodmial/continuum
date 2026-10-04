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


function nonActionableCautionIsMergeable(review) {
  // A complete review can legitimately choose merge_with_caution solely for
  // external/live verification that cannot be repaired on this HEAD. Re-running
  // the same review cannot create new machine evidence and previously caused an
  // endless rereview loop. Only accept that caution when the structured review
  // explicitly proves there is no security concern and no ticket non-compliance.
  const security = String(review && review.security_concerns || '').trim().toLowerCase();
  if (!['no', 'none', 'false', 'n/a', 'na', '-'].includes(security)) return false;

  const ticket = review && review.ticket_compliance_check;
  if (!Array.isArray(ticket) || ticket.length === 0) return false;
  const empty = new Set(['', '-', 'none', 'n/a', 'na', 'no']);
  for (const entry of ticket) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return false;
    if (!Object.prototype.hasOwnProperty.call(entry, 'not_compliant_requirements')) return false;
    const value = String(entry.not_compliant_requirements ?? '').trim().toLowerCase();
    if (!empty.has(value)) return false;
  }
  return true;
}

function reviewDisposition(reviewPayload, improveJsonl, threshold = IMPROVE_REPAIR_THRESHOLD) {
  const review = reviewPayload && reviewPayload.review ? reviewPayload.review : reviewPayload;
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
  const qualifying = qualifyingImproveSuggestions(improveJsonl, threshold);
  const reviewCount = review.key_issues_to_review.length;
  const common = {
    recommendation,
    reviewCount,
    qualifyingSuggestionCount: qualifying.length,
  };
  if (reviewCount > 0 || qualifying.length > 0) {
    return {
      ...common,
      action: 'repair',
      reason:
        reviewCount + ' review finding(s), ' +
        qualifying.length + ' qualifying improve suggestion(s)',
    };
  }
  if (
    recommendation === 'safe_to_merge' ||
    (recommendation === 'merge_with_caution' && nonActionableCautionIsMergeable(review))
  ) {
    return {
      ...common,
      action: 'merge',
      reason:
        recommendation === 'safe_to_merge'
          ? 'safe_to_merge with no actionable review/improve items'
          : 'non-actionable caution with explicit no-security/no-noncompliance evidence',
    };
  }
  return {
    ...common,
    action: 'rereview',
    reason: 'blocking merge recommendation without actionable payload: ' + recommendation,
  };
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
  CONTROLLER_STATE_MARKER,
  IMPROVE_REPAIR_THRESHOLD,
  buildRepairBatch,
  controllerStateBody,
  logicalFingerprint,
  normalizeProblem,
  overlappingLocation,
  parseImproveJsonl,
  qualifyingImproveSuggestions,
  reviewDisposition,
  nonActionableCautionIsMergeable,
  sameLogicalDefect,
};
