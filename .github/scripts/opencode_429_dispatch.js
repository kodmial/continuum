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

module.exports = {
  DEFAULT_CANDIDATES,
  normalizeWorkflowPath,
  workflowIdFor,
  workflowSupportsDispatch,
  workflowSupportsWorkflowRun,
  resolveDispatchWorkflow,
};
