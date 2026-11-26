'use strict';

// Repo-local routing for the OpenCode 429 watchdog (kodmial/continuum#350).
//
// The watchdog must dispatch the *caller* that owns a real
// `workflow_dispatch` trigger, never the reusable engine
// (`continuum-opencode.yml` exposes only `workflow_call` in Continuum's own
// checkout). Continuum self-dogfood names its caller `opencode.yml`, while an
// installed consumer receives `continuum-opencode.yml`; neither name is hard
// coded here. Resolution prefers explicit trusted configuration, then the path
// of the completed run that woke the watchdog, then the two known caller names,
// and accepts a candidate only when its checked-in YAML really declares
// `workflow_dispatch`.

const DEFAULT_CANDIDATES = [
  '.github/workflows/opencode.yml',
  '.github/workflows/continuum-opencode.yml',
];

function normalizeWorkflowPath(value) {
  let text = String(value || '').trim();
  if (!text) return '';
  text = text.replace(/\\/g, '/');
  if (text.startsWith('./')) text = text.slice(2);
  if (!text.startsWith('.github/workflows/')) {
    if (text.includes('/')) return '';
    text = '.github/workflows/' + text;
  }
  if (!/\.ya?ml$/i.test(text)) return '';
  if (text.includes('..')) return '';
  return text;
}

function workflowIdFor(path) {
  const normalized = normalizeWorkflowPath(path);
  if (!normalized) return '';
  return normalized.slice(normalized.lastIndexOf('/') + 1);
}

function topLevelOnBlock(content) {
  const lines = String(content || '').split(/\r?\n/);
  const start = lines.findIndex((line) => /^on:\s*(?:#.*)?$/.test(line));
  if (start === -1) return null;
  const block = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\S/.test(line) && !line.startsWith('#')) break;
    block.push(line);
  }
  return block.join('\n');
}

function workflowSupportsDispatch(content) {
  const block = topLevelOnBlock(content);
  if (block === null) return false;
  return /^\s{2}workflow_dispatch:\s*(?:#.*)?$/m.test(block);
}

function workflowSupportsWorkflowRun(content) {
  const block = topLevelOnBlock(content);
  if (block === null) return false;
  return /^\s{2}workflow_run:\s*(?:#.*)?$/m.test(block);
}

// Resolve the dispatch target from trusted inputs.
//
// `workflows` maps a normalized repository path to either its YAML text or
// `null` when the path does not exist. `configured` is the optional
// repository variable value (bare name or full path). `watchedRunPath` is the
// `workflow_run.path` that delivered the completed run.
function resolveDispatchWorkflow({ configured, watchedRunPath, workflows } = {}) {
  const contents = workflows || {};
  const ordered = [];
  const seen = new Set();
  const push = (path, source) => {
    const normalized = normalizeWorkflowPath(path);
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    ordered.push({ path: normalized, source });
  };
  push(configured, 'configured');
  push(watchedRunPath, 'watched-run');
  for (const candidate of DEFAULT_CANDIDATES) push(candidate, 'fallback');

  for (const candidate of ordered) {
    if (!Object.prototype.hasOwnProperty.call(contents, candidate.path)) continue;
    const content = contents[candidate.path];
    if (typeof content !== 'string') continue;
    if (!workflowSupportsDispatch(content)) continue;
    return {
      path: candidate.path,
      workflowId: workflowIdFor(candidate.path),
      source: candidate.source,
      watchedRunTrigger: workflowSupportsWorkflowRun(content),
    };
  }
  return null;
}

async function observeDispatchedRun({
  target, notBefore, displayTitle, recoveryRunName, oldRunId,
  adopting = false, owner, repo, listWorkflowRuns, warn, sleep,
}) {
  // No invented recent floor: without a real claim timestamp, an
  // inventory cannot prove that an earlier dispatch is absent.
  if (!Number.isFinite(notBefore) || notBefore <= 0 ||
      notBefore > Date.now() + 5000) return { incomplete: true };
  const lower = Math.floor(Math.max(0, notBefore - 5000) / 1000);
  // Keep the entire claim-to-now history in scope, not an arbitrary
  // hour. GitHub's filtered search can cap at 1,000; bisect saturated
  // inclusive second-resolution windows, oldest first, under a
  // per-observation REST budget. Never scan unrelated history.
  // Two complete scans are required for `adopting`; a shared 40-call
  // budget can starve the confirmation scan on dense inventories and
  // permanently retain the claim. Give adoption room for both scans.
  let remaining = adopting ? 80 : 40;
  const candidates = runs => runs.filter(candidate => {
    if (Number(candidate.id) === Number(oldRunId || 0)) return false;
    if (candidate.event !== 'workflow_dispatch') return false;
    if (candidate.path && candidate.path !== target.path) return false;
    return new Date(candidate.created_at || 0).getTime() + 5000 >= notBefore;
  });
  const byTitle = (runs, title) => title
    ? runs.find(candidate => candidate.display_title === title)
    : null;
  async function scanInventory() {
    const upper = Math.ceil(Date.now() / 1000);
    const pending = [[lower, upper]];
    const inventory = new Map();
    while (pending.length) {
      const [start, end] = pending.pop();
      const created = new Date(start * 1000).toISOString() + '..' +
        new Date(end * 1000).toISOString();
      const local = new Map();
      let total = null;
      for (let page = 1; page <= 10; page += 1) {
        if (remaining <= 0) {
          return { incomplete: true, reason: 'budget-exhausted' };
        }
        remaining -= 1;
        const { data } = await listWorkflowRuns({
          owner, repo, workflow_id: target.workflowId,
          event: 'workflow_dispatch', created, per_page: 100, page,
        });
        if (!Array.isArray(data?.workflow_runs) ||
            !Number.isSafeInteger(data.total_count) || data.total_count < 0) {
          return { incomplete: true, reason: 'malformed-page' };
        }
        const pageRuns = data.workflow_runs;
        // Positive exact identity is valid even on a capped page;
        // fallback matching and negative evidence require completeness.
        const exact = byTitle(candidates(pageRuns).filter(candidate => {
          const at = Math.floor(new Date(candidate.created_at || 0).getTime() / 1000);
          return /^\d+$/.test(String(candidate.id || '')) &&
            Number.isFinite(at) && at >= start && at <= end;
        }), recoveryRunName);
        if (exact) return { match: exact };
        if (page === 1) {
          total = data.total_count;
          // A reported 1,000 may itself be the search ceiling.
          // Split even at equality; a complete inventory of
          // exactly 1,000 distinct runs can then be reconciled.
          if (total >= 1000) {
            // A single second cannot be subdivided any further, so its
            // saturation is not a transient read failure.
            if (start === end) {
              return { incomplete: true, reason: 'saturated-second' };
            }
            const midpoint = Math.floor((start + end) / 2);
            pending.push([midpoint + 1, end], [start, midpoint]);
            break;
          }
        } else if (data.total_count !== total) {
          return { incomplete: true, reason: 'count-drift' };
        }
        for (const candidate of pageRuns) {
          const id = String(candidate.id || '');
          const at = Math.floor(new Date(candidate.created_at || 0).getTime() / 1000);
          if (!/^\d+$/.test(id) || local.has(id) || inventory.has(id) ||
              !Number.isFinite(at) || at < start || at > end ||
              candidate.event !== 'workflow_dispatch') {
            return { incomplete: true, reason: 'invalid-run' };
          }
          local.set(id, candidate);
        }
        if (local.size === total) {
          for (const [id, candidate] of local) inventory.set(id, candidate);
          break;
        }
        if (local.size > total || pageRuns.length < 100 || page === 10) {
          return { incomplete: true, reason: 'inconsistent-page' };
        }
      }
    }
    return { runs: candidates([...inventory.values()]) };
  }
  let complete = false;
  let consecutiveComplete = 0;
  for (let attempt = 0; attempt < 10 && remaining > 0; attempt += 1) {
    // Negative evidence is a point-in-time observation. A later
    // failed/truncated read invalidates it; only the exact positive
    // match may settle ownership regardless of scan completeness.
    complete = false;
    try {
      const result = await scanInventory();
      if (result.match) return result.match;
      if (result.incomplete) {
        // A transient inconsistency invalidates only the current read;
        // retry within the remaining budget instead of giving up after
        // one. A saturated single second cannot be subdivided, so a
        // retry reproduces the same result and would only burn budget.
        consecutiveComplete = 0;
        complete = false;
        if (result.reason === 'saturated-second' || remaining <= 0) {
          return { incomplete: true };
        }
        if (attempt < 9) await sleep(5000);
        continue;
      }
      const runs = result.runs;
      const match = byTitle(runs, displayTitle) ||
        (runs.length === 1 ? runs[0] : null);
      if (match) return match;
      complete = true;
      consecutiveComplete += 1;
      // An expired claim has already waited its lease. Two fresh,
      // consecutive complete observations, without a later failed
      // read, establish absence without rewalking dense inventories
      // ten times. Post-dispatch observation keeps its usual poll.
      if (adopting && consecutiveComplete >= 2) return null;
    } catch (err) {
      consecutiveComplete = 0;
      // This is GitHub REST, not the OpenCode provider. An access or rate
      // refusal cannot prove absence; defer to the next guarded wake.
      if ([403, 429].includes(Number(err?.status))) {
        warn('GitHub REST refused dispatch observation; keeping recovery claim.');
        return { incomplete: true };
      }
      warn(
        'OpenCode 429 dispatch observation attempt failed for ' +
        target.path + ': ' + (err.message || err)
      );
    }
    if (attempt < 9 && remaining > 0) {
      await sleep(5000);
    }
  }
  return complete && (!adopting || consecutiveComplete >= 2)
    ? null : { incomplete: true };
}

module.exports = {
  observeDispatchedRun,
  DEFAULT_CANDIDATES,
  normalizeWorkflowPath,
  workflowIdFor,
  workflowSupportsDispatch,
  workflowSupportsWorkflowRun,
  resolveDispatchWorkflow,
};
