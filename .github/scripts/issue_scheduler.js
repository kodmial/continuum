'use strict';

module.exports = async function runIssueScheduler({ github, context, core }) {
  const { execFileSync } = require('child_process');
  const fs = require('fs');
  const path = require('path');
  const owner = context.repo.owner;
  const repo = context.repo.repo;
  const repoFullName = owner + '/' + repo;
  const readToken = String(process.env.READ_GITHUB_TOKEN || '').trim();
  if (!readToken) {
    throw new Error(
      'READ_GITHUB_TOKEN is required for same-repository scheduler reads.'
    );
  }
  const readBaseUrl = (
    github?.request?.endpoint?.DEFAULTS?.baseUrl ||
    process.env.GITHUB_API_URL ||
    'https://api.github.com'
  );
  const commandParser = path.join(
    process.env.GITHUB_WORKSPACE || '',
    '.continuum-engine',
    '.github',
    'scripts',
    'opencode_command.py'
  );
  // The scheduler never reimplements the command grammar. One
  // parser call classifies a whole comment page, and every owner
  // command decision below consumes that canonical result.
  function classifyComments(bodies) {
    const output = execFileSync(
      'python3',
      [commandParser, 'classify-json'],
      {
        input: JSON.stringify(bodies),
        encoding: 'utf8',
        maxBuffer: 16 * 1024 * 1024,
      }
    );
    const result = JSON.parse(output);
    if (!Array.isArray(result) || result.length !== bodies.length) {
      throw new Error(
        'Canonical command parser returned an unexpected batch result.'
      );
    }
    return result;
  }
  const readGithub = new github.constructor({
    auth: readToken,
    baseUrl: readBaseUrl,
  });
  const childWorkerWorkflow =
    process.env.CHILD_WORKER_WORKFLOW || 'continuum-child-worker.yml';
  const childActiveWipParsed = Number.parseInt(
    process.env.CHILD_ACTIVE_WIP || '0',
    10
  );
  if (!Number.isInteger(childActiveWipParsed) || childActiveWipParsed < 0) {
    throw new Error(
      'Invalid CHILD_ACTIVE_WIP: ' + process.env.CHILD_ACTIVE_WIP
    );
  }
  const childCandidatesFile = (
    process.env.CHILD_CANDIDATES_FILE || ''
  ).trim();
  // Active-run discovery is authoritative for duplicate dispatch
  // (kodmial/continuum#387). `degraded` means the status-filtered
  // Actions view failed or was truncated: never invent an empty
  // active inventory. Delegated coding stops for this pass while
  // Parent-local scheduling continues and the next wake retries.
  const activeRunsState = (
    process.env.CHILD_ACTIVE_RUNS_STATE || 'ok'
  ).trim();
  if (!['ok', 'degraded'].includes(activeRunsState)) {
    throw new Error(
      'Invalid CHILD_ACTIVE_RUNS_STATE: ' + activeRunsState
    );
  }
  const activeRunsScript = (
    process.env.CHILD_ACTIVE_RUNS_SCRIPT || ''
  ).trim();
  // Delegation state is the authoritative split between
  // Parent-local and delegated scheduling (kodmial/continuum#326).
  // `degraded` means delegated discovery was unavailable: Child
  // dispatch stays fail-closed while Parent-local work continues,
  // and an empty inventory is never fabricated as a success.
  const delegationState = (
    process.env.DELEGATION_STATE || 'none'
  ).trim();
  if (!['ok', 'degraded', 'none'].includes(delegationState)) {
    throw new Error('Invalid DELEGATION_STATE: ' + delegationState);
  }
  const childActiveWip =
    activeRunsState === 'ok' ? childActiveWipParsed : 0;
  let childCandidates = [];
  if (delegationState === 'ok') {
    if (activeRunsState === 'degraded') {
      core.warning(
        'Delegated active-run discovery is unavailable or truncated; ' +
        'new delegated coding dispatch is disabled for this pass. ' +
        'Parent-local scheduling and PR-Agent Child review continue, ' +
        'and the next scheduler wake retries automatically.'
      );
    } else {
      if (!childCandidatesFile || !fs.existsSync(childCandidatesFile)) {
        throw new Error(
          'Delegation state is "ok" but the delegated child candidate file is missing.'
        );
      }
      childCandidates = JSON.parse(
        fs.readFileSync(childCandidatesFile, 'utf8')
      );
      if (!Array.isArray(childCandidates)) {
        throw new Error('Delegated child candidate payload is not an array.');
      }
    }
  } else if (delegationState === 'degraded') {
    core.warning(
      'Delegated child discovery is unavailable; Child dispatch is ' +
      'disabled for this pass while Parent-local scheduling continues.'
    );
  }
  
  // Fresh pre-dispatch recheck against the same bounded,
  // status-filtered discovery. The recheck snapshot is taken at
  // most once per scheduler pass and reused for every delegated
  // candidate, so REST cost is bounded by active-run volume rather
  // than candidate count (no per-Child N+1). Exit 0 = an identical
  // task/review run is still active, 1 = not active, anything else
  // is an uncertain outage and must also suppress the duplicate.
  const freshActiveRunsFile = (
    process.env.CHILD_ACTIVE_RUNS_FILE ||
    path.join(
      process.env.RUNNER_TEMP || '.',
      'continuum-active-runs-fresh.tsv'
    )
  ).trim();
  let freshActiveRunsReady = false;
  let freshActiveRunsUnavailable = false;
  function ensureFreshDelegatedActiveRuns() {
    if (freshActiveRunsReady || freshActiveRunsUnavailable) {
      return freshActiveRunsReady;
    }
    if (!activeRunsScript) {
      freshActiveRunsUnavailable = true;
      core.warning(
        'Delegated active-run recheck is unavailable; skipping ' +
        'delegated dispatch for this pass.'
      );
      return false;
    }
    try {
      execFileSync(
        'bash',
        [activeRunsScript, 'snapshot', '--output', freshActiveRunsFile],
        {
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'pipe'],
          env: {
            ...process.env,
            GH_TOKEN: readToken,
            GITHUB_REPOSITORY: repoFullName,
          },
        }
      );
      freshActiveRunsReady = true;
      return true;
    } catch (error) {
      freshActiveRunsUnavailable = true;
      core.warning(
        'Delegated active-run recheck failed for this pass; ' +
        'skipping delegated dispatch because an unknown run ' +
        'state must not create a duplicate.'
      );
      return false;
    }
  }
  function freshDelegatedTaskActive(childId, taskNumber) {
    if (!ensureFreshDelegatedActiveRuns()) {
      return true;
    }
    for (const kind of ['task', 'review']) {
      try {
        execFileSync(
          'bash',
          [
            activeRunsScript,
            'task-active',
            '--file',
            freshActiveRunsFile,
            '--child-id',
            String(childId),
            '--kind',
            kind,
            '--task',
            String(taskNumber),
          ],
          {
            encoding: 'utf8',
            stdio: ['ignore', 'pipe', 'pipe'],
            env: {
              ...process.env,
              GH_TOKEN: readToken,
              GITHUB_REPOSITORY: repoFullName,
            },
          }
        );
        return true;
      } catch (error) {
        if (error && error.status === 1) {
          continue;
        }
        core.warning(
          'Delegated active-run recheck failed for SubTask #' +
          taskNumber + '; skipping dispatch for this pass because ' +
          'an unknown run state must not create a duplicate.'
        );
        return true;
      }
    }
    return false;
  }
  
  // Same-pass dispatch identity: a selected task that has just
  // been dispatched must never be launched again even before its
  // new run becomes visible in a fresh listing.
  const dispatchedTaskKeys = new Set();
  
  function positiveInt(name, fallback) {
    const value = Number.parseInt(process.env[name] || String(fallback), 10);
    if (!Number.isInteger(value) || value < 1) {
      throw new Error('Invalid ' + name + ': ' + process.env[name]);
    }
    return value;
  }
  
  const wipLimit = positiveInt('WIP_LIMIT', 2);
  const leaseMinutes = positiveInt('LEASE_MINUTES', 45);
  const maxDispatchAttempts = positiveInt('MAX_DISPATCH_ATTEMPTS', 2);
  const leaseMs = leaseMinutes * 60 * 1000;
  // A recent owner /oc is only a race guard while GitHub starts the
  // corresponding workflow. It must not inherit the full work lease:
  // cancelled/premature runs may legitimately become eligible again
  // as soon as their native blockers close.
  const commandGraceMinutes = positiveInt('COMMAND_GRACE_MINUTES', 5);
  const commandGraceMs = commandGraceMinutes * 60 * 1000;
  
  const priorityOrder = [
    'priority:p0',
    'priority:p1',
    'priority:p2',
  ];
  const priorityRank = new Map(
    priorityOrder.map((name, index) => [name, index])
  );
  const inProgressLabel = process.env.IN_PROGRESS_LABEL || 'automation:in-progress';
  const pausedLabel = process.env.PAUSE_LABEL || 'automation:paused';
  const qualifyingLabel = process.env.QUALIFYING_LABEL || 'automation:qualifying';
  const blockedLabel = process.env.BLOCKED_LABEL || 'automation:blocked';
  const dispatchMarker = process.env.DISPATCH_MARKER || '<!-- issue-scheduler-dispatch -->';
  const postPauseComment =
    (process.env.POST_PAUSE_COMMENT || 'true') !== 'false';
  const resetMarkers =
    (process.env.RESET_MARKERS || 'false') === 'true';
  const requirePriorityLabel =
    (process.env.REQUIRE_PRIORITY_LABEL || 'false') === 'true';
  // Optional admission gate (kodmial/continuum#266): empty keeps the
  // current behavior unchanged; when configured, automatic dispatch
  // admits only issues carrying this label. Priority stays a ranking
  // signal only, and qualification dispatch plus manual owner `/oc`
  // never require it.
  const readyLabel = (process.env.READY_LABEL || '').trim();
  const opencodeWorkflowName =
    process.env.OPENCODE_WORKFLOW_NAME || 'OpenCode agent';
  const opencodeWorkflowPath =
    process.env.OPENCODE_WORKFLOW_PATH ||
    '.github/workflows/continuum-opencode.yml';
  const dispatchRef = process.env.DISPATCH_REF || 'main';
  
  // A ready issue normally reaches OpenCode as an owner comment, the
  // only path that needs no extra permission. A consumer whose
  // OpenCode caller has no `issue_comment` trigger needs the
  // workflow_dispatch form instead.
  const opencodeDispatch = (
    process.env.OPENCODE_DISPATCH || 'comment'
  ).trim();
  if (!['comment', 'workflow'].includes(opencodeDispatch)) {
    throw new Error(
      "Unsupported opencode_dispatch '" + opencodeDispatch +
        "'; expected 'comment' or 'workflow'."
    );
  }
  
  // Label-driven execution routes are configuration, never a table
  // inside core: the render and Docker qualification controllers are
  // separate reusable workflows that a consumer may or may not
  // install, and a consumer may route to workflows core has never
  // heard of. Empty means "route nothing".
  //
  // A malformed entry fails the run instead of being dropped,
  // because a silently ignored route looks reserved but is never
  // executed.
  const executionRoutes = (process.env.EXECUTION_LABEL_ROUTES || '')
    .split('\n')
    .map(line => line.trim())
    .filter(line => line && !line.startsWith('#'))
    .map(line => {
      const parts = line.split('|').map(part => part.trim());
      if (parts.length < 2 || parts.length > 3 || !parts[0] || !parts[1]) {
        throw new Error(
          "Invalid execution_label_routes line: '" +
            line +
            "'; expected '<issue label>|<workflow file name>|<input>=<value>,...'."
        );
      }
      const inputs = {};
      for (const assignment of (parts[2] || '').split(',')) {
        const trimmed = assignment.trim();
        if (!trimmed) continue;
        const separator = trimmed.indexOf('=');
        if (separator <= 0) {
          throw new Error(
            "Invalid execution_label_routes input '" +
              trimmed +
              "' on line '" +
              line +
              "'; expected '<name>=<value>'."
          );
        }
        inputs[trimmed.slice(0, separator).trim()] = trimmed
          .slice(separator + 1)
          .trim();
      }
      return { label: parts[0], workflow: parts[1], inputs };
    });
  const executionRouteLabels = new Set(
    executionRoutes.map(route => route.label)
  );
  
  const childDispatchWorkflow = (
    process.env.CHILD_DISPATCH_WORKFLOW || ''
  ).trim();
  const countOpenPrsAsWip =
    (process.env.COUNT_OPEN_PRS_AS_WIP || 'true') !== 'false';
  const pauseOnFailure =
    (process.env.PAUSE_ON_FAILURE || 'false') !== 'false';
  
  // Byte-for-byte the marker the watchdog parses. A doubled backslash
  // here would silently disable declared-dependency blocking.
  function declaredDependencyNumbers(issue) {
    const match = (issue.body || '').match(
      /<!--\s*automation-blocked-by:\s*([0-9#\s,]+?)\s*-->/i
    );
    if (!match) return [];
    return [...new Set(
      [...match[1].matchAll(/\d+/g)]
        .map(item => Number.parseInt(item[0], 10))
        .filter(Number.isInteger)
    )];
  }
  
  // Pass-level open-issue snapshot for non-mutating admission
  // (Work-Lock #58 item 6 A1/A5). Populated once from the primary
  // open-issue pagination as the complete open inventory (issues
  // and PRs alike). Same-repository snapshot admission only: an
  // issue present in the snapshot is open; an issue absent from
  // the complete snapshot is not an open local blocker. This
  // avoids one issues.get per declared blocker during candidate
  // selection. Fresh per-blocker reads still run immediately
  // before every dispatch; cross-repository child checks stay on
  // the PAT path below and never consult this snapshot.
  let openSnapshotByNumber = null;
  // Flipped once snapshot admission ends so the just-in-time
  // pre-dispatch guards below always re-read instead of reusing
  // pass-start state. Call strings stay unchanged on purpose.
  let useOpenSnapshot = true;
  function rememberOpenSnapshot(items) {
    openSnapshotByNumber = new Map(
      (items || []).map(item => [item.number, item])
    );
  }
  function updateOpenSnapshot(issue) {
    if (!openSnapshotByNumber) return;
    if (!issue || typeof issue.number !== 'number') return;
    if (issue.state === 'closed') {
      openSnapshotByNumber.delete(issue.number);
    } else {
      openSnapshotByNumber.set(issue.number, issue);
    }
  }
  function invalidateOpenSnapshot() {
    openSnapshotByNumber = null;
  }
  
  // Non-mutating native-blocker snapshot (Work-Lock #58 item 6
  // A2), memoized by local issue number so reservation
  // reconciliation and candidate selection share one read per
  // issue. The PAT-backed qualification check keeps its own
  // credential-scoped entry; neither cache is consulted by the
  // fresh pre-dispatch revalidation, which always re-reads.
  const nativeBlockerSnapshotRead = new Map();
  const nativeBlockerSnapshotPat = new Map();
  async function snapshotNativeBlockers(issueNumber) {
    if (nativeBlockerSnapshotRead.has(issueNumber)) {
      return nativeBlockerSnapshotRead.get(issueNumber);
    }
    const blockers = await readGithub.paginate(
      'GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by',
      {
        owner,
        repo,
        issue_number: issueNumber,
        per_page: 100,
        headers: { 'X-GitHub-Api-Version': '2022-11-28' },
      }
    );
    nativeBlockerSnapshotRead.set(issueNumber, blockers);
    return blockers;
  }
  async function snapshotNativeBlockersPat(issueNumber) {
    if (nativeBlockerSnapshotPat.has(issueNumber)) {
      return nativeBlockerSnapshotPat.get(issueNumber);
    }
    const blockers = await github.paginate(
      'GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by',
      {
        owner,
        repo,
        issue_number: issueNumber,
        per_page: 100,
        headers: { 'X-GitHub-Api-Version': '2022-11-28' },
      }
    );
    nativeBlockerSnapshotPat.set(issueNumber, blockers);
    return blockers;
  }
  function invalidateNativeBlockers(issueNumber) {
    if (issueNumber === undefined) {
      nativeBlockerSnapshotRead.clear();
      nativeBlockerSnapshotPat.clear();
    } else {
      nativeBlockerSnapshotRead.delete(issueNumber);
      nativeBlockerSnapshotPat.delete(issueNumber);
    }
  }
  
  // Bounded recent owner-command guard (Work-Lock #58 item 6 A3).
  // The final dispatch guard only cares about an owner /oc or
  // /opencode inside COMMAND_GRACE_MINUTES, so it queries the
  // smallest safe recent window (grace plus a small clock-skew
  // margin via the API since parameter) instead of paginating
  // full comment history. Owner identity, both command spellings
  // and the exact age comparison are preserved; the query is
  // always fresh, never a cached snapshot.
  function commandGraceSinceIso() {
    const skewMs = 2 * 60 * 1000;
    return new Date(Date.now() - commandGraceMs - skewMs).toISOString();
  }
  async function recentOwnerCommandWithinGrace(issueNumber) {
    const comments = await readGithub.paginate(
      readGithub.rest.issues.listComments,
      {
        owner,
        repo,
        issue_number: issueNumber,
        per_page: 100,
        since: commandGraceSinceIso(),
      }
    );
    const commandClasses = classifyComments(
      comments.map(comment => comment.body || '')
    );
    return comments
      .filter((comment, index) =>
        comment.user?.login === owner &&
        commandClasses[index] === 'run'
      )
      .sort(
        (a, b) =>
          new Date(a.created_at).getTime() -
          new Date(b.created_at).getTime()
      )
      .at(-1) || null;
  }
  
  // Terminal generation-stop gate (kodmial/continuum#309). A
  // truly drifted/tampered generation publishes one typed stop
  // comment keyed by (repo, issue, generation, pinned/live
  // hashes, reason) carrying digests only. Every scheduler path
  // checks it before dispatching so a stopped generation is
  // never redispatched, never consumes WIP, and never emits
  // another /oc comment. Trust mirrors the snapshot contract
  // (OWNER/MEMBER/COLLABORATOR, github-actions[bot], or the
  // repository owner login). The stop is evaluated over the
  // full comment history via pagination: a stop beyond the
  // first page still gates, mirroring the bash
  // delegated/legacy gates. A strictly later explicit owner
  // recovery marker
  // (continuum-task-generation-recover), which is the sole
  // in-place re-arm. A successor issue or an explicit new
  // generation needs no recovery marker. automation:blocked
  // semantics are untouched: this is a dedicated stop identity,
  // never a dependency marker.
  const generationStopMarkerRe =
    /<!--\s*continuum-task-generation-stop\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s+pinned-spec=([0-9a-fA-F]{64})\s+live-spec=([0-9a-fA-F]{64})\s+reason=(drift|tampered)\s*-->/;
  const generationRecoverMarkerRe =
    /<!--\s*continuum-task-generation-recover\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s*-->/;
  const operatorStopMarkerRe =
    /<!--\s*continuum-task-operator-stop\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s*-->/;
  const operatorResumeMarkerRe =
    /<!--\s*continuum-task-operator-resume\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s*-->/;
  function isTrustedStopComment(comment, trustedOwner = owner) {
    const association = String(
      comment.author_association || ''
    ).toUpperCase();
    if (['OWNER', 'MEMBER', 'COLLABORATOR'].includes(association)) return true;
    const login = String(comment.user?.login || '').toLowerCase();
    if (login === 'github-actions[bot]') return true;
    return login === String(trustedOwner || '').toLowerCase();
  }
  function stopOrderKey(entry) {
    return (entry.created_at || '') + '#' + String(entry.id || 0).padStart(12, '0');
  }
  async function terminalGenerationStop(
    targetOwner, targetRepo, issueNumber, client, generation = 1
  ) {
    const idle = { stopped: false };
    const wantedRepo = (targetOwner + '/' + targetRepo).toLowerCase();
    const wantedGeneration = Number(generation) || 1;
    let first;
    try {
      const response = await client.rest.issues.listComments({
        owner: targetOwner,
        repo: targetRepo,
        issue_number: issueNumber,
        per_page: 100,
        page: 1,
      });
      first = response.data;
    } catch (err) {
      core.warning(
        'Terminal-stop lookup failed for #' + issueNumber +
        '; keeping it eligible for this pass.'
      );
      return idle;
    }
    const scan = (list) => {
      const stops = [];
      const recovers = [];
      const operatorStops = [];
      const operatorResumes = [];
      for (const comment of list || []) {
        if (!isTrustedStopComment(comment, targetOwner)) continue;
        const body = String(comment.body || '');
        const operatorStopMatch = body.match(operatorStopMarkerRe);
        if (
          operatorStopMatch &&
          operatorStopMatch[1].toLowerCase() === wantedRepo &&
          Number(operatorStopMatch[2]) === issueNumber &&
          Number(operatorStopMatch[3]) === wantedGeneration
        ) {
          operatorStops.push({
            created_at: comment.created_at || '',
            id: comment.id || 0,
          });
          continue;
        }
        const operatorResumeMatch = body.match(operatorResumeMarkerRe);
        if (
          operatorResumeMatch &&
          operatorResumeMatch[1].toLowerCase() === wantedRepo &&
          Number(operatorResumeMatch[2]) === issueNumber &&
          Number(operatorResumeMatch[3]) === wantedGeneration
        ) {
          operatorResumes.push({
            created_at: comment.created_at || '',
            id: comment.id || 0,
          });
          continue;
        }
        const stopMatch = body.match(generationStopMarkerRe);
        if (
          stopMatch &&
          stopMatch[1].toLowerCase() === wantedRepo &&
          Number(stopMatch[2]) === issueNumber &&
          Number(stopMatch[3]) === wantedGeneration
        ) {
          stops.push({
            created_at: comment.created_at || '',
            id: comment.id || 0,
            generation: wantedGeneration,
            pinnedSpec: stopMatch[4].toLowerCase(),
            liveSpec: stopMatch[5].toLowerCase(),
            reason: stopMatch[6].toLowerCase(),
          });
          continue;
        }
        const recoverMatch = body.match(generationRecoverMarkerRe);
        if (
          recoverMatch &&
          recoverMatch[1].toLowerCase() === wantedRepo &&
          Number(recoverMatch[2]) === issueNumber &&
          Number(recoverMatch[3]) === wantedGeneration
        ) {
          recovers.push({
            created_at: comment.created_at || '',
            id: comment.id || 0,
          });
        }
      }
      return { stops, recovers, operatorStops, operatorResumes };
    };
    const sortByOrder = list =>
      list.sort((a, b) => (stopOrderKey(a) < stopOrderKey(b) ? -1 : 1));
    const effectiveOperatorStop = state => {
      sortByOrder(state.operatorStops);
      sortByOrder(state.operatorResumes);
      if (state.operatorStops.length === 0) return null;
      if (
        state.operatorResumes.length > 0 &&
        stopOrderKey(state.operatorResumes[state.operatorResumes.length - 1]) >
          stopOrderKey(state.operatorStops[state.operatorStops.length - 1])
      ) {
        return null;
      }
      return state.operatorStops[0];
    };
    const operatorResult = () => ({
      stopped: true,
      generation: wantedGeneration,
      kind: 'operator',
      reason: 'operator',
      pinnedSpec: '',
      liveSpec: '',
    });
    let found = scan(first);
    sortByOrder(found.stops);
    sortByOrder(found.recovers);
    sortByOrder(found.operatorStops);
    sortByOrder(found.operatorResumes);
    // A stop beyond the first page must still gate: always
    // evaluate the full history below instead of returning
    // idle when the first page carries no stop.
    let scanned;
    try {
      const all = await client.paginate(
        client.rest.issues.listComments,
        {
          owner: targetOwner,
          repo: targetRepo,
          issue_number: issueNumber,
          per_page: 100,
        }
      );
      scanned = scan(all);
    } catch (err) {
      if (effectiveOperatorStop(found)) {
        return operatorResult();
      }
      if (found.stops.length === 0) {
        core.warning(
          'Terminal-stop lookup failed for #' + issueNumber +
          '; keeping it eligible for this pass.'
        );
        return idle;
      }
      core.warning(
        'Terminal-stop recovery lookup failed for #' + issueNumber +
        '; treating the generation as stopped.'
      );
      const earliest = found.stops[0];
      return {
        stopped: true,
        generation: wantedGeneration,
        kind: 'generation',
        pinnedSpec: earliest.pinnedSpec,
        liveSpec: earliest.liveSpec,
        reason: earliest.reason,
      };
    }
    sortByOrder(scanned.stops);
    sortByOrder(scanned.recovers);
    sortByOrder(scanned.operatorStops);
    sortByOrder(scanned.operatorResumes);
    if (effectiveOperatorStop(scanned)) {
      return operatorResult();
    }
    if (scanned.stops.length === 0) return idle;
    if (
      scanned.recovers.length > 0 &&
      stopOrderKey(scanned.recovers[scanned.recovers.length - 1]) > stopOrderKey(scanned.stops[scanned.stops.length - 1])
    ) {
      return idle;
    }
    const earliest = scanned.stops[0];
    return {
      stopped: true,
      generation: wantedGeneration,
      kind: 'generation',
      pinnedSpec: earliest.pinnedSpec,
      liveSpec: earliest.liveSpec,
      reason: earliest.reason,
    };
  }
  function terminalStopSkipReason(issueNumber, stop) {
    const stopGeneration = Number(stop.generation) || 1;
    if (stop.kind === 'operator' || stop.reason === 'operator') {
      return 'operator generation stop: #' + issueNumber +
        ' generation ' + stopGeneration +
        ' was cancelled by the repository owner; scheduler, watchdog, ' +
        '429 recovery, label changes, and automated retries must not ' +
        'redispatch it. Resume only with an explicit operator resume ' +
        'or a new generation number.';
    }
    return 'terminal generation stop: #' + issueNumber +
      ' generation ' + stopGeneration + ' is stopped (' + (stop.reason || 'drift') +
      ' pinned ' + String(stop.pinnedSpec || 'unknown').slice(0, 12) +
      ' live ' + String(stop.liveSpec || 'unknown').slice(0, 12) +
      '); open a successor issue with an explicit dependency or ' +
      'start an explicit new generation instead of redispatching.';
  }
  
  const ownerResumeMarkerRe =
    /^[ \t]*<!--\s*continuum-resume-after:v1\s+predecessor=([1-9]\d*)\s*-->[ \t]*(?:\r?\n|$)/;
  const ownerResumeMarkerLineRe =
    /^[ \t]*<!--\s*continuum-resume-after:v1\s+predecessor=([1-9]\d*)\s*-->[ \t]*$/gm;
  const ownerResumeMarkerShapeRe =
    /continuum-resume-after(?::v1)?\b/;
  const ownerResumeMarkerShapeCountRe =
    /continuum-resume-after(?::v1)?\b/g;
  const ownerResumeRecoveryRe =
    /<!--\s*continuum-429-(?:recovery|resumption|backoff)\b/;
  const ownerResumeCommandRe =
    /^[ ]{0,3}\/(?:opencode|oc)(?=\s|$)/m;
  const ownerResumeTaskContextRe =
    /<!--\s*continuum-task-context\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s*-->/g;
  const ownerResumeSnapshotRefRe =
    /<!--\s*continuum-task-snapshot-ref\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s+title-sha256=([0-9a-fA-F]{64})\s+body-sha256=([0-9a-fA-F]{64})\s+spec-sha256=([0-9a-fA-F]{64})\s*-->/g;
  const ownerResumeSnapshotMarkerRe =
    /<!--\s*continuum-task-snapshot\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s+title-sha256=([0-9a-fA-F]{64})\s+body-sha256=([0-9a-fA-F]{64})\s+spec-sha256=([0-9a-fA-F]{64})\s*-->/g;
  const ownerResumeCommentPageLimit = 5;
  const ownerResumeCiWorkflow =
    (process.env.OWNER_RESUME_CI_WORKFLOW || 'CI').trim();
  // The scheduler workflow always forwards this knob, falling back to the
  // Continuum default. A consumer repository opts in to the Contract Gate by
  // setting CONTINUUM_CONTRACT_GATE_WORKFLOW_NAME to a non-default name; while
  // the knob stays at the default the gate remains Continuum-only.
  const ownerResumeContractGateDefaultWorkflow =
    'Continuum Contract Gate';
  const ownerResumeContractGateWorkflow =
    (process.env.OWNER_RESUME_CONTRACT_GATE_WORKFLOW ||
      ownerResumeContractGateDefaultWorkflow).trim();
  const ownerResumeContractGateConfigured =
    ownerResumeContractGateWorkflow !== ownerResumeContractGateDefaultWorkflow;
  const ownerResumeDefaultBranch = (
    process.env.DEFAULT_BRANCH ||
    context.payload.repository?.default_branch ||
    'main'
  ).trim();
  
  function ownerResumeLogin(comment) {
    return String(comment?.user?.login || '').toLowerCase();
  }
  function ownerResumeCreatedAt(comment) {
    return new Date(comment?.created_at || 0).getTime();
  }
  function ownerResumeMarkerComments(comments) {
    return (comments || []).filter(comment =>
      ownerResumeMarkerShapeRe.test(String(comment.body || ''))
    );
  }
  function ownerResumeScan(targetNumber, comments) {
    const shaped = ownerResumeMarkerComments(comments);
    if (shaped.length === 0) {
      return { instruction: null, reason: 'not owner-armed' };
    }
    if (shaped.length !== 1) {
      return {
        instruction: null,
        reason: 'duplicate or conflicting owner-resume markers',
      };
    }
    const comment = shaped[0];
    if (ownerResumeLogin(comment) !== owner.toLowerCase()) {
      return {
        instruction: null,
        reason: 'owner-resume marker author is not the repository owner',
      };
    }
    const body = String(comment.body || '');
    if ((body.match(ownerResumeMarkerShapeCountRe) || []).length !== 1) {
      return {
        instruction: null,
        reason: 'duplicate or conflicting owner-resume markers',
      };
    }
    const match = body.match(ownerResumeMarkerRe);
    if (!match || (body.match(ownerResumeMarkerLineRe) || []).length !== 1) {
      return {
        instruction: null,
        reason: 'owner-resume marker syntax is not supported',
      };
    }
    const predecessor = Number.parseInt(match[1], 10);
    if (
      !Number.isInteger(predecessor) ||
      predecessor < 1 ||
      predecessor === targetNumber
    ) {
      return {
        instruction: null,
        reason: 'owner-resume marker predecessor is invalid',
      };
    }
    return {
      instruction: { predecessor, comment },
      reason: 'owner-authorized resume',
    };
  }
  function ownerResumeBaselineNumber(comments, targetNumber) {
    const scan = ownerResumeScan(targetNumber, comments);
    if (!scan.instruction) return null;
    const at = ownerResumeCreatedAt(scan.instruction.comment);
    return Number.isFinite(at) && at > 0 ? at : null;
  }
  function ownerResumePrSnapshotBinding(pr, predecessorNumber) {
    const body = String(pr?.body || '');
    const contexts = [...body.matchAll(ownerResumeTaskContextRe)];
    const refs = [...body.matchAll(ownerResumeSnapshotRefRe)];
    if (contexts.length === 0 && refs.length === 0) return null;
    if (contexts.length > 1 || refs.length > 1) return false;
    if (contexts.length === 1) {
      const context = contexts[0];
      if (String(context[1]).toLowerCase() !== repoFullName.toLowerCase()) {
        return false;
      }
      if (Number(context[2]) !== predecessorNumber) return false;
    }
    if (refs.length === 0) return null;
    const ref = refs[0];
    if (String(ref[1]).toLowerCase() !== repoFullName.toLowerCase()) {
      return false;
    }
    if (Number(ref[2]) !== predecessorNumber) return false;
    if (String(ref[3]) !== '1') return false;
    return {
      title: String(ref[4]).toLowerCase(),
      body: String(ref[5]).toLowerCase(),
      spec: String(ref[6]).toLowerCase(),
    };
  }
  function ownerResumeBoundPr(pr, predecessorNumber) {
    const branchBound = String(pr?.head?.ref || '').startsWith(
      'opencode/issue' + predecessorNumber + '-'
    );
    const binding = ownerResumePrSnapshotBinding(pr, predecessorNumber);
    if (binding === false) return null;
    if (!branchBound && binding === null) return null;
    return { branchBound, binding };
  }
  function ownerResumeSnapshotBindingMatches(
    comments, predecessorNumber, binding
  ) {
    for (const comment of comments || []) {
      const association = String(
        comment.author_association || ''
      ).toUpperCase();
      const login = ownerResumeLogin(comment);
      if (
        login !== owner.toLowerCase() &&
        login !== 'github-actions[bot]' &&
        !['OWNER', 'MEMBER', 'COLLABORATOR'].includes(association)
      ) {
        continue;
      }
      const markers = [
        ...String(comment.body || '').matchAll(ownerResumeSnapshotMarkerRe)
      ];
      for (const marker of markers) {
        if (
          String(marker[1]).toLowerCase() !== repoFullName.toLowerCase() ||
          Number(marker[2]) !== predecessorNumber ||
          String(Number(marker[3])) !== '1'
        ) {
          continue;
        }
        if (
          String(marker[4]).toLowerCase() === binding.title &&
          String(marker[5]).toLowerCase() === binding.body &&
          String(marker[6]).toLowerCase() === binding.spec
        ) {
          return true;
        }
      }
    }
    return false;
  }
  function ownerResumeActivityAfter(comments, instruction) {
    const baseline = ownerResumeCreatedAt(instruction.comment);
    return (comments || []).some(comment => {
      if (String(comment.id || '') === String(instruction.comment.id || '')) {
        return false;
      }
      if (!(ownerResumeCreatedAt(comment) > baseline)) return false;
      const body = String(comment.body || '');
      const login = ownerResumeLogin(comment);
      const trustedFencer =
        login === 'github-actions[bot]' ||
        login === owner.toLowerCase();
      if (trustedFencer && body.includes(dispatchMarker)) return true;
      if (trustedFencer && ownerResumeRecoveryRe.test(body)) return true;
      return (
        login === owner.toLowerCase() &&
        ownerResumeCommandRe.test(body)
      );
    });
  }
  function ownerResumeGenerationStopped(comments, targetNumber) {
    const wantedRepo = repoFullName.toLowerCase();
    let lastStop = null;
    let lastRecover = null;
    for (const comment of comments || []) {
      const association = String(
        comment.author_association || ''
      ).toUpperCase();
      const login = ownerResumeLogin(comment);
      if (
        login !== owner.toLowerCase() &&
        login !== 'github-actions[bot]' &&
        !['OWNER', 'MEMBER', 'COLLABORATOR'].includes(association)
      ) {
        continue;
      }
      const body = String(comment.body || '');
      const stop = body.match(generationStopMarkerRe);
      if (
        stop &&
        stop[1].toLowerCase() === wantedRepo &&
        Number(stop[2]) === targetNumber &&
        Number(stop[3]) === 1
      ) {
        const key = stopOrderKey(comment);
        if (!lastStop || key > lastStop) lastStop = key;
        continue;
      }
      const recover = body.match(generationRecoverMarkerRe);
      if (
        recover &&
        recover[1].toLowerCase() === wantedRepo &&
        Number(recover[2]) === targetNumber &&
        Number(recover[3]) === 1
      ) {
        const key = stopOrderKey(comment);
        if (!lastRecover || key > lastRecover) lastRecover = key;
      }
    }
    return lastStop !== null && (!lastRecover || lastRecover < lastStop);
  }
  async function ownerResumeIssueComments(issueNumber) {
    const comments = [];
    let truncated = false;
    for (let page = 1; page <= ownerResumeCommentPageLimit; page += 1) {
      const { data } = await readGithub.rest.issues.listComments({
        owner,
        repo,
        issue_number: issueNumber,
        per_page: 100,
        page,
        sort: 'created',
        direction: 'desc',
      });
      comments.push(...data);
      if (data.length < 100) {
        truncated = false;
        break;
      }
      truncated = page === ownerResumeCommentPageLimit;
    }
    if (truncated) {
      // Probe one page beyond the bound so an exactly-full window
      // is not mistaken for a truncated comment stream.  Any
      // remaining comments mean the scan is incomplete, and an
      // incomplete scan must never authorize or fence a resume.
      const { data: overflow } =
        await readGithub.rest.issues.listComments({
          owner,
          repo,
          issue_number: issueNumber,
          per_page: 100,
          page: ownerResumeCommentPageLimit + 1,
          sort: 'created',
          direction: 'desc',
        });
      if (overflow.length > 0) {
        core.notice(
          'Owner-armed resume comment scan for #' + issueNumber +
            ' was truncated after ' + comments.length +
            ' newest comments; failing closed.'
        );
        throw new Error(
          'comment stream truncated after ' + comments.length +
            ' newest comments'
        );
      }
    }
    return comments;
  }
  function newestOwnerResumeRun(runs, headSha, workflowNames, workflowPaths) {
    const names = new Set(workflowNames.filter(Boolean));
    const paths = new Set(workflowPaths.filter(Boolean));
    const matching = (runs || []).filter(run =>
      String(run.head_sha || '').toLowerCase() === headSha &&
      (names.has(String(run.name || '')) ||
       paths.has(String(run.path || '')))
    );
    if (matching.length === 0) return null;
    matching.sort((a, b) => {
      const at = new Date(a.run_started_at || a.created_at || 0).getTime();
      const bt = new Date(b.run_started_at || b.created_at || 0).getTime();
      if (at !== bt) return at - bt;
      return Number(a.id || 0) - Number(b.id || 0);
    });
    return matching[matching.length - 1];
  }
  function ownerResumeIndependentReview(reviews, statuses, headSha, prAuthor) {
    const cleanStatus = (statuses || []).some(status =>
      String(status.context || '') === 'continuum/pr-agent-review' &&
      String(status.state || '').toLowerCase() === 'success' &&
      String(status.description || '').trim() ===
        'PR-Agent review complete: clean'
    );
    if (cleanStatus) return true;
    const author = String(prAuthor || '').toLowerCase();
    return (reviews || []).some(review => {
      const login = String(review.user?.login || '').toLowerCase();
      return (
        String(review.state || '').toUpperCase() === 'APPROVED' &&
        String(review.commit_id || '').toLowerCase() === headSha &&
        login &&
        login !== author &&
        !login.endsWith('[bot]') &&
        String(review.author_association || '').toUpperCase() !== 'BOT'
      );
    });
  }
  async function ownerResumeDecision(issue, instruction, comments) {
    const targetNumber = issue.number;
    const labels =
      labelsByIssue.get(targetNumber) || new Set();
    if (!readyLabel || !labels.has(readyLabel) || !labels.has(pausedLabel)) {
      return { state: 'NOOP', reason: 'target is not paused and admitted' };
    }
    if (labels.has('no-auto-merge') || labels.has(blockedLabel)) {
      return { state: 'NOOP', reason: 'target carries an explicit stop label' };
    }
    if (openPrIssues.has(targetNumber) || activeRunIssues.has(targetNumber)) {
      return {
        state: 'NOOP',
        reason: 'target already has an open implementation PR or run',
      };
    }
    if (ownerResumeGenerationStopped(comments, targetNumber)) {
      return { state: 'NOOP', reason: 'target generation has an owner STOP' };
    }
    if (ownerResumeActivityAfter(comments, instruction)) {
      return {
        state: 'NOOP',
        reason: 'a post-marker run or recovery attempt already owns this target',
      };
    }
  
    const predecessorNumber = instruction.predecessor;
    let predecessor;
    try {
      predecessor = (await readGithub.rest.issues.get({
        owner,
        repo,
        issue_number: predecessorNumber,
      })).data;
    } catch (err) {
      core.warning(
        'Owner-armed resume predecessor read failed for #' +
          predecessorNumber + '; failing closed: ' + err.message
      );
      return { state: 'NOOP', reason: 'predecessor state is unavailable' };
    }
    if (predecessor.pull_request) {
      return { state: 'NOOP', reason: 'predecessor is a pull request' };
    }
    if (String(predecessor.state || '').toLowerCase() === 'open') {
      return {
        state: 'WAIT',
        reason: 'predecessor issue #' + predecessorNumber + ' is still open',
      };
    }
    if (
      String(predecessor.state || '').toLowerCase() !== 'closed' ||
      String(predecessor.state_reason || '').toLowerCase() !== 'completed'
    ) {
      return {
        state: 'NOOP',
        reason: 'predecessor is not closed as completed',
      };
    }
  
    const bound = [];
    for (const candidatePr of pulls) {
      if (candidatePr.head?.repo?.full_name !== repoFullName) continue;
      const bindingInfo = ownerResumeBoundPr(
        candidatePr, predecessorNumber
      );
      if (!bindingInfo) continue;
      bound.push({ pr: candidatePr, ...bindingInfo });
    }
    if (bound.length === 0) {
      return {
        state: 'NOOP',
        reason: 'predecessor has no trusted implementation PR binding',
      };
    }
    bound.sort(
      (a, b) => Number(a.pr.number || 0) - Number(b.pr.number || 0)
    );
    const latest = bound[bound.length - 1].pr;
    if (!latest.merged_at || String(latest.state || '').toLowerCase() !== 'closed') {
      return {
        state: 'NOOP',
        reason: 'latest predecessor implementation PR is not merged',
      };
    }
  
    let fullPr;
    try {
      fullPr = (await readGithub.rest.pulls.get({
        owner,
        repo,
        pull_number: latest.number,
      })).data;
    } catch (err) {
      core.warning(
        'Owner-armed resume PR read failed for #' + latest.number +
          '; failing closed: ' + err.message
      );
      return { state: 'NOOP', reason: 'predecessor PR state is unavailable' };
    }
    const fullBindingInfo = ownerResumeBoundPr(
      fullPr, predecessorNumber
    );
    if (!fullBindingInfo) {
      return {
        state: 'NOOP',
        reason: 'predecessor PR binding is not trusted',
      };
    }
    if (
      !fullPr.merged ||
      !fullPr.merged_at ||
      String(fullPr.state || '').toLowerCase() !== 'closed'
    ) {
      return {
        state: 'NOOP',
        reason: 'predecessor implementation PR is not truly merged',
      };
    }
    if (fullBindingInfo.binding) {
      let predecessorComments;
      try {
        predecessorComments = await ownerResumeIssueComments(
          predecessorNumber
        );
      } catch (err) {
        core.warning(
          'Owner-armed resume predecessor snapshot read failed for #' +
            predecessorNumber + '; failing closed: ' + err.message
        );
        return {
          state: 'NOOP',
          reason: 'predecessor snapshot evidence is unavailable',
        };
      }
      if (
        !ownerResumeSnapshotBindingMatches(
          predecessorComments,
          predecessorNumber,
          fullBindingInfo.binding
        )
      ) {
        return {
          state: 'NOOP',
          reason: 'predecessor PR snapshot binding is not trusted',
        };
      }
    }
    if (String(fullPr.base?.ref || '') !== ownerResumeDefaultBranch) {
      return {
        state: 'NOOP',
        reason: 'predecessor PR base is not the expected default branch',
      };
    }
    if (fullPr.head?.repo?.full_name !== repoFullName) {
      return {
        state: 'NOOP',
        reason: 'predecessor PR head repository is not trusted',
      };
    }
    const headSha = String(fullPr.head?.sha || '').toLowerCase();
    const mergeSha = String(fullPr.merge_commit_sha || '').toLowerCase();
    if (
      !/^[0-9a-f]{40}(?:[0-9a-f]{24})?$/.test(headSha) ||
      !/^[0-9a-f]{40}(?:[0-9a-f]{24})?$/.test(mergeSha)
    ) {
      return {
        state: 'NOOP',
        reason: 'predecessor merge or HEAD SHA is missing or stale',
      };
    }
  
    let exactHeadRuns = [];
    try {
      exactHeadRuns = await readGithub.paginate(
        readGithub.rest.actions.listWorkflowRunsForRepo,
        { owner, repo, head_sha: headSha, per_page: 100 }
      );
    } catch (err) {
      core.warning(
        'Owner-armed resume exact-HEAD run read failed for ' +
          headSha + '; failing closed: ' + err.message
      );
      return { state: 'NOOP', reason: 'exact-HEAD run evidence is unavailable' };
    }
    const ownerResumeRequiredRuns = [
      {
        names: [ownerResumeCiWorkflow],
        paths: ['.github/workflows/' + 'ci.yml'],
        label: 'CI',
      },
    ];
    // The Contract Gate workflow exists only in the Continuum control-plane
    // repository by default; consumer repositories never run it, so requiring
    // its run here would make owner-armed resume permanently NOOP there. An
    // explicit non-default workflow-name override applies the gate in any
    // repository.
    if (
      ownerResumeContractGateConfigured ||
      String(repoFullName || '').toLowerCase() === 'kodmial/continuum'
    ) {
      ownerResumeRequiredRuns.push({
        names: [ownerResumeContractGateWorkflow],
        paths: ['.github/workflows/' + 'contract-gate.yml'],
        label: 'Contract Gate',
      });
    }
    for (const required of ownerResumeRequiredRuns) {
      const run = newestOwnerResumeRun(
        exactHeadRuns, headSha, required.names, required.paths
      );
      if (!run) {
        return {
          state: 'NOOP',
          reason: required.label + ' evidence is missing on the PR HEAD',
        };
      }
      if (
        String(run.status || '').toLowerCase() !== 'completed' ||
        String(run.conclusion || '').toLowerCase() !== 'success'
      ) {
        return {
          state: 'NOOP',
          reason: required.label + ' is not successful on the PR HEAD',
        };
      }
    }
  
    let statuses = [];
    let reviews = [];
    try {
      statuses = (await readGithub.rest.repos.getCombinedStatusForRef({
        owner,
        repo,
        ref: headSha,
      })).data.statuses || [];
    } catch (err) {
      core.warning(
        'Owner-armed resume review status read failed for ' +
          headSha + '; failing closed: ' + err.message
      );
      return {
        state: 'NOOP',
        reason: 'independent review evidence is unavailable',
      };
    }
    try {
      reviews = await readGithub.paginate(
        readGithub.rest.pulls.listReviews,
        { owner, repo, pull_number: fullPr.number, per_page: 100 }
      );
    } catch (err) {
      core.warning(
        'Owner-armed resume review read failed for #' +
          fullPr.number + '; failing closed: ' + err.message
      );
      return {
        state: 'NOOP',
        reason: 'independent review evidence is unavailable',
      };
    }
    if (!ownerResumeIndependentReview(
      reviews,
      statuses,
      headSha,
      fullPr.user?.login || ''
    )) {
      return {
        state: 'NOOP',
        reason: 'independent clean review evidence is missing',
      };
    }
    return {
      state: 'RESUME',
      reason: 'predecessor #' + predecessorNumber + ' is complete',
      pullRequest: fullPr.number,
      headSha,
    };
  }
  
              async function openDeclaredBlockers(
    issue,
    targetOwner = owner,
    targetRepo = repo
  ) {
    const open = [];
    const sameRepo =
      targetOwner === owner && targetRepo === repo;
    for (const blockerNumber of declaredDependencyNumbers(issue)) {
      if (blockerNumber === issue.number) continue;
      if (sameRepo && useOpenSnapshot && openSnapshotByNumber) {
        const snap = openSnapshotByNumber.get(blockerNumber);
        if (snap && snap.state === 'open') open.push(snap);
        continue;
      }
      const readClient =
        sameRepo ? readGithub : github;
      const response = await readClient.rest.issues.get({
        owner: targetOwner,
        repo: targetRepo,
        issue_number: blockerNumber,
      });
      if (response.data.state === 'open') open.push(response.data);
    }
    return open;
  }
  
  // Mandatory live-qualification gate: implementation -> qualification
  // -> capability completion. The deterministic rules live in
  // src/continuum/qualification.py; this embedding mirrors them for
  // the scheduler runtime. Byte-for-byte markers: a doubled
  // backslash here would silently disable the whole gate.
  function qualificationRefs(body, selfNumber) {
    if (typeof body !== 'string' || !body) return [];
    const found = new Set();
    const pattern = /<!--\s*automation-qualification:\s*([0-9#\s,]+?)\s*-->/gi;
    for (const match of body.matchAll(pattern)) {
      for (const item of match[1].matchAll(/\d+/g)) {
        const number = Number.parseInt(item[0], 10);
        if (!Number.isInteger(number) || number <= 0) continue;
        if (number === selfNumber) continue;
        found.add(number);
      }
    }
    return [...found].sort((a, b) => a - b);
  }
  
  // Normal implementation blockers. Mirrors
  // src/continuum/qualification.py parse_blocked_by_refs: only the
  // machine-readable marker counts, prose never does.
  function blockedByRefs(body, selfNumber) {
    if (typeof body !== 'string' || !body) return [];
    const found = new Set();
    const pattern = /<!--\s*automation-blocked-by:\s*([0-9#\s,]+?)\s*-->/gi;
    for (const match of body.matchAll(pattern)) {
      for (const item of match[1].matchAll(/\d+/g)) {
        const number = Number.parseInt(item[0], 10);
        if (!Number.isInteger(number) || number <= 0) continue;
        if (number === selfNumber) continue;
        found.add(number);
      }
    }
    return [...found].sort((a, b) => a - b);
  }
  
  // Qualification DAG semantics (kodmial/continuum#278). Mirrors
  // src/continuum/qualification.py: the validator is never a normal
  // downstream implementation dependency. A direct
  // capability<->qualification back-edge is detected and
  // deterministically ignored for qualification readiness with a
  // clear diagnostic, never honored as a blocker.
  function qualificationCycleEdges(
    capabilityNumber, refs, capabilityBlockedBy, qualificationBlockedBy
  ) {
    const refSet = new Set(refs || []);
    const blockerSet = new Set(capabilityBlockedBy || []);
    const forward = [...refSet].filter(number => blockerSet.has(number)).sort((a, b) => a - b);
    const reverse = (refs || []).filter(ref =>
      ((qualificationBlockedBy || {})[ref] || []).includes(capabilityNumber)
    ).sort((a, b) => a - b);
    return {
      capabilityBlockedByQualification: forward,
      qualificationsBlockedByCapability: reverse,
    };
  }
  
  function hasQualificationBlockerCycle(
    capabilityNumber, refs, capabilityBlockedBy, qualificationBlockedBy
  ) {
    const edges = qualificationCycleEdges(
      capabilityNumber, refs, capabilityBlockedBy, qualificationBlockedBy
    );
    return edges.capabilityBlockedByQualification.length > 0 ||
      edges.qualificationsBlockedByCapability.length > 0;
  }
  
  // Capability blockers with qualification back-edges ignored.
  // Mirrors effective_qualification_blockers: an open blocker that
  // is itself a declared qualification stays reported in `ignored`
  // for the diagnostic while `effective` drives the decision.
  function effectiveQualificationBlockers(refs, openBlockers) {
    const refSet = new Set(refs || []);
    const blockers = (openBlockers || []).map(blocker =>
      typeof blocker === 'object' && blocker !== null && typeof blocker.number === 'number'
        ? blocker.number
        : blocker
    );
    return {
      effective: blockers.filter(number => !refSet.has(number)),
      ignored: blockers.filter(number => refSet.has(number)),
    };
  }
  
  // Reverse-edge bypass for one qualification tracker. Mirrors
  // qualification_dispatch_bypass: a tracker blocked only by the
  // capability it validates still dispatches for that exact run.
  function qualificationDispatchBypass(
    qualificationNumber, capabilityNumber, openBlockers
  ) {
    const blockers = (openBlockers || []).map(blocker =>
      typeof blocker === 'object' && blocker !== null && typeof blocker.number === 'number'
        ? blocker.number
        : blocker
    ).filter(number => number !== qualificationNumber);
    const bypassed = blockers.filter(number => number === capabilityNumber);
    const effective = blockers.filter(number => number !== capabilityNumber);
    if (effective.length > 0) return { effective, bypassed, reason: 'blocked' };
    if (bypassed.length > 0) {
      return { effective, bypassed, reason: 'dispatch-ignoring-capability-back-edge' };
    }
    return { effective, bypassed, reason: 'dispatch' };
  }
  
  // Every issue number declared as a qualification anywhere in the
  // open inventory. Trackers need explicit qualification dispatch
  // and are never auto-admitted to generic implementation,
  // independent of automation:ready. Mirrors
  // collect_qualification_trackers.
  function collectQualificationTrackers(issues) {
    const found = new Set();
    for (const issue of issues || []) {
      for (const ref of qualificationRefs(issue.body || '', issue.number)) {
        found.add(ref);
      }
    }
    return [...found].sort((a, b) => a - b);
  }
  
  function shouldAdmitToImplementation(issueNumber, trackerNumbers) {
    if ((trackerNumbers || []).includes(issueNumber)) {
      return { admit: false, reason: 'qualification-tracker-needs-explicit-dispatch' };
    }
    return { admit: true, reason: 'ready' };
  }
  
  // Trusted reusable result tuple for one qualification at one SHA.
  // Mirrors qualification_result_tuple: consumable without tracker
  // closure, so a reusable validation issue stays open across
  // revisions while each revision carries its own evidence.
  function qualificationResultTuple(commentBodies, qualificationNumber, requiredSha) {
    const sha = normalizeSha(requiredSha);
    if (!sha) return null;
    let latest = null;
    for (const comment of commentBodies || []) {
      if (!isTrustedQualificationComment(comment, true)) continue;
      const body = commentBodyOf(comment);
      if (typeof body !== 'string' || !body) continue;
      if (commentHasDispatchMarker(body)) continue;
      for (const entry of parseQualificationEvidenceEntries(body)) {
        if (entry.issue != null && entry.issue !== qualificationNumber) continue;
        if (entry.sha !== sha) continue;
        latest = {
          issue: qualificationNumber,
          sha,
          result: entry.result,
          fingerprint: entry.fingerprint || null,
          source: entry.source,
        };
      }
    }
    return latest;
  }
  
  function hasTrustedPassForSha(commentBodies, qualificationNumber, requiredSha) {
    const tuple = qualificationResultTuple(
      commentBodies, qualificationNumber, requiredSha
    );
    return !!tuple && tuple.result === 'pass';
  }
  
  function isValidSha(value) {
    return typeof value === 'string' && /^[0-9a-f]{40}$/i.test(value.trim());
  }
  
  function normalizeSha(value) {
    if (!isValidSha(value)) return null;
    return value.trim().toLowerCase();
  }
  
  function latestRequiredSha(body, capabilityNumber) {
    if (typeof body !== 'string' || !body) return null;
    let latest = null;
    const pattern = /<!--\s*continuum-qualification-required\s+capability\s*=\s*(\d+)\s+sha\s*=\s*([0-9a-f]{40})\s*-->/gi;
    for (const match of body.matchAll(pattern)) {
      if (capabilityNumber != null && Number(match[1]) !== capabilityNumber) continue;
      latest = match[2].toLowerCase();
    }
    return latest;
  }
  
  function requiredShaMarker(capabilityNumber, sha) {
    return '<!-- continuum-qualification-required capability=' +
      capabilityNumber + ' sha=' + sha + ' -->';
  }
  
  function qualificationDispatchMarker(capabilityNumber, qualificationNumber, sha) {
    return '<!-- continuum-qualification-dispatch capability=' +
      capabilityNumber + ' qualification=' + qualificationNumber +
      ' sha=' + sha + ' -->';
  }
  
  const TRUSTED_AUTHOR_ASSOCIATIONS = new Set(['OWNER', 'MEMBER', 'COLLABORATOR']);
  const TRUSTED_AUTOMATION_LOGINS = new Set(['github-actions[bot]']);
  
  function commentBodyOf(comment) {
    if (typeof comment === 'string') return comment;
    if (comment && typeof comment.body === 'string') return comment.body;
    return null;
  }
  
  function commentAssociationOf(comment) {
    if (typeof comment === 'string') return null;
    const direct = comment?.author_association;
    if (typeof direct === 'string' && direct) return direct.toUpperCase();
    const nested = comment?.author?.association || comment?.user?.association;
    if (typeof nested === 'string' && nested) return nested.toUpperCase();
    return null;
  }
  
  function commentLoginOf(comment) {
    if (typeof comment === 'string') return null;
    const login = comment?.author?.login || comment?.user?.login ||
      comment?.author_login || comment?.login;
    return typeof login === 'string' && login ? login : null;
  }
  
  // Only trusted repository actors/automation may drive the
  // lifecycle. Public/untrusted forged markers are rejected.
  // Plain strings predate author metadata and stay trusted.
  function isTrustedQualificationComment(comment, allowAutomation) {
    if (typeof comment === 'string') return true;
    const association = commentAssociationOf(comment);
    if (TRUSTED_AUTHOR_ASSOCIATIONS.has(association)) return true;
    if (allowAutomation) {
      const login = (commentLoginOf(comment) || '').toLowerCase();
      if (TRUSTED_AUTOMATION_LOGINS.has(login)) return true;
    }
    return false;
  }
  
  function commentHasDispatchMarker(body) {
    if (typeof body !== 'string' || !body) return false;
    return /<!--\s*continuum-qualification-dispatch\s+capability\s*=\s*(\d+)\s+qualification\s*=\s*(\d+)\s+sha\s*=\s*([0-9a-f]{40})\s*-->/i.test(body);
  }
  
  function isQualificationResultCommentBody(body) {
    if (typeof body !== 'string' || !body) return false;
    if (/<!--\s*continuum-qualification-result\b/i.test(body)) return true;
    return body.includes('<!-- continuum-docker-qualification-result -->') ||
      body.includes('<!-- continuum-render-qualification-result -->');
  }
  
  function hasQualificationDispatch(bodies, capabilityNumber, qualificationNumber, sha) {
    const wanted = normalizeSha(sha);
    if (!wanted) return false;
    const pattern = /<!--\s*continuum-qualification-dispatch\s+capability\s*=\s*(\d+)\s+qualification\s*=\s*(\d+)\s+sha\s*=\s*([0-9a-f]{40})\s*-->/gi;
    for (const comment of bodies || []) {
      if (!isTrustedQualificationComment(comment, true)) continue;
      const body = commentBodyOf(comment);
      if (typeof body !== 'string') continue;
      for (const match of body.matchAll(pattern)) {
        if (
          Number(match[1]) === capabilityNumber &&
          Number(match[2]) === qualificationNumber &&
          match[3].toLowerCase() === wanted
        ) {
          return true;
        }
      }
    }
    return false;
  }
  
  // Machine-readable evidence only: the canonical marker, or a
  // Docker/Render controller payload whose head SHA matches. Prose
  // verdicts never count, and entries for any other SHA are stale.
  // A dispatch/instruction comment never counts as evidence, even
  // when its prose quotes a marker shape.
  function parseQualificationEvidenceEntries(commentBody) {
    if (typeof commentBody !== 'string' || !commentBody) return [];
    const entries = [];
    const canonical = /<!--\s*continuum-qualification-result\b([^>]*?)-->/gi;
    for (const match of commentBody.matchAll(canonical)) {
      const attrs = {};
      for (const attr of (match[1] || '').matchAll(/([A-Za-z_]+)\s*=\s*([^\s>]+)/g)) {
        attrs[attr[1].toLowerCase()] = attr[2];
      }
      const sha = normalizeSha(attrs.sha || attrs.head);
      let verdict = null;
      for (const key of ['result', 'classification', 'verdict']) {
        if (attrs[key] == null) continue;
        const text = String(attrs[key]).toLowerCase();
        verdict = ['pass', 'passed', 'success', 'approved'].includes(text)
          ? 'pass'
          : 'fail';
        break;
      }
      if (!sha || !verdict) continue;
      let issue = null;
      if (attrs.issue != null) {
        issue = Number.parseInt(String(attrs.issue), 10);
        if (!Number.isInteger(issue)) continue;
      }
      entries.push({
        issue,
        sha,
        result: verdict,
        fingerprint: attrs.fingerprint || attrs.fp || attrs.digest || null,
      });
    }
    for (const marker of [
      '<!-- continuum-docker-qualification-result -->',
      '<!-- continuum-render-qualification-result -->',
    ]) {
      const at = commentBody.indexOf(marker);
      if (at === -1) continue;
      const tail = commentBody.slice(at + marker.length).split('-->')[0].trim();
      let payload = null;
      try {
        payload = JSON.parse(tail.startsWith('{') ? tail : tail);
      } catch (err) {
        payload = null;
      }
      if (payload && typeof payload === 'object') {
        const sha = normalizeSha(
          payload.head_sha || payload.sha || payload.head
        );
        const classification = String(
          payload.classification || payload.result || payload.verdict || ''
        ).toLowerCase();
        if (sha && classification) {
          entries.push({
            issue: null,
            sha,
            result: ['pass', 'passed', 'success', 'approved'].includes(classification)
              ? 'pass'
              : 'fail',
            fingerprint: (payload.fingerprint || payload.fp || payload.digest || null),
          });
        }
      }
    }
    return entries;
  }
  
  function qualificationEvidenceState(commentBodies, qualificationNumber, requiredSha) {
    const sha = normalizeSha(requiredSha);
    if (!sha) return 'unknown';
    let latest = null;
    for (const comment of commentBodies || []) {
      if (!isTrustedQualificationComment(comment, true)) continue;
      const body = commentBodyOf(comment);
      if (typeof body !== 'string' || !body) continue;
      if (commentHasDispatchMarker(body)) continue;
      for (const entry of parseQualificationEvidenceEntries(body)) {
        if (entry.issue != null && entry.issue !== qualificationNumber) continue;
        if (entry.sha !== sha) continue;
        latest = entry.result;
      }
    }
    if (latest === 'pass') return 'pass';
    if (latest === 'fail') return 'fail';
    return 'unknown';
  }
  
  function latestTrustedEvidenceBody(commentBodies, qualificationNumber, requiredSha) {
    const sha = normalizeSha(requiredSha);
    if (!sha) return null;
    let latest = null;
    for (const comment of commentBodies || []) {
      if (!isTrustedQualificationComment(comment, true)) continue;
      const body = commentBodyOf(comment);
      if (typeof body !== 'string' || !body) continue;
      if (commentHasDispatchMarker(body)) continue;
      let matched = false;
      for (const entry of parseQualificationEvidenceEntries(body)) {
        if (entry.issue != null && entry.issue !== qualificationNumber) continue;
        if (entry.sha === sha) matched = true;
      }
      if (matched) latest = body;
    }
    return latest;
  }
  
  let cachedMainSha = null;
  async function currentMainSha() {
    if (cachedMainSha) return cachedMainSha;
    const { data } = await readGithub.rest.repos.getBranch({
      owner,
      repo,
      branch: 'main',
    });
    cachedMainSha = data.commit.sha;
    return cachedMainSha;
  }
  
  async function issueCommentsBodies(issueNumber) {
    const comments = await readGithub.paginate(
      readGithub.rest.issues.listComments,
      { owner, repo, issue_number: issueNumber, per_page: 100 }
    );
    return comments.map(comment => ({
      body: comment.body || '',
      author_association: comment.author_association || '',
      author: { login: comment.user?.login || '' },
      user: { login: comment.user?.login || '' },
    }));
  }
  
  // Dispatch one mandatory qualification for an exact main SHA.
  // Pause never strands the lifecycle: a paused qualification is
  // unpaused here, not skipped. Idempotent per SHA so duplicate
  // merge events cannot start qualification storms; a closed
  // qualification is never dispatched (it must be reopened first).
  // Explicit qualification dispatch is independent of the optional
  // automation:ready admission gate and bypasses normal
  // implementation-readiness admission for the declared capability
  // relation (kodmial/continuum#278): a tracker blocked only by its
  // own capability still dispatches for that exact run. The
  // immutable required SHA and every qualification-specific safety
  // rule still apply.
  async function dispatchQualificationIssue(
    capabilityNumber,
    qualificationNumber,
    requiredSha
  ) {
    const sha = normalizeSha(requiredSha);
    if (!sha) return 'no-required-sha';
    let qual;
    try {
      qual = (await readGithub.rest.issues.get({
        owner,
        repo,
        issue_number: qualificationNumber,
      })).data;
    } catch (err) {
      if (err.status === 404) return 'qualification-missing';
      throw err;
    }
    if (qual.pull_request) return 'qualification-missing';
    if (qual.state !== 'open') return 'qualification-closed';
    rememberIssue(qual);
  
    // A qualification tracker may have its own implementation
    // prerequisites. Explicit qualification dispatch bypasses only
    // the back-edge to the capability it validates; every other
    // declared/native blocker must remain closed before execution.
    const declaredQualificationBlockers = await openDeclaredBlockers(qual);
    const declaredQualificationGate = qualificationDispatchBypass(
      qualificationNumber,
      capabilityNumber,
      declaredQualificationBlockers.map(blocker => blocker.number)
    );
    if (declaredQualificationGate.effective.length > 0) {
      core.info(
        'Qualification #' + qualificationNumber + ' waits for tracker blocker(s) ' +
        declaredQualificationGate.effective.map(number => '#' + number).join(', ') + '.'
      );
      return 'qualification-blocked';
    }
    if (declaredQualificationGate.bypassed.length > 0) {
      core.notice(
        'Qualification #' + qualificationNumber +
        ': ignoring capability back-edge #' + capabilityNumber +
        ' while preserving all other tracker blockers.'
      );
    }
  
    let nativeQualificationBlockers = [];
    try {
      nativeQualificationBlockers =
        await snapshotNativeBlockersPat(qualificationNumber);
    } catch (err) {
      if (err.status !== 404) throw err;
    }
    const nativeQualificationGate = qualificationDispatchBypass(
      qualificationNumber,
      capabilityNumber,
      nativeQualificationBlockers
        .filter(blocker => blocker.state === 'open')
        .map(blocker => blocker.number)
    );
    if (nativeQualificationGate.effective.length > 0) {
      core.info(
        'Qualification #' + qualificationNumber + ' waits for native blocker(s) ' +
        nativeQualificationGate.effective.map(number => '#' + number).join(', ') + '.'
      );
      return 'qualification-blocked';
    }
  
    const qualLabels = labelsByIssue.get(qualificationNumber) || new Set();
    const wasPaused = qualLabels.has(pausedLabel);
    await removeLabel(qualificationNumber, pausedLabel);
    const bodies = await issueCommentsBodies(qualificationNumber);
    if (
      hasQualificationDispatch(
        bodies, capabilityNumber, qualificationNumber, sha
      )
    ) {
      return wasPaused ? 'unpaused-already-dispatched' : 'already-dispatched';
    }
    const marker = qualificationDispatchMarker(
      capabilityNumber, qualificationNumber, sha
    );
    // Instructional prose must never embed a parseable result
    // marker: the evidence parser scans comment bodies, so a
    // literal canonical marker here would self-satisfy the gate
    // for the exact SHA. Describe the marker without its
    // HTML-comment brackets.
    const note =
      'Mandatory live qualification for capability #' + capabilityNumber +
      ' at main `' + sha +
      '`: automatically dispatched after implementation merge. ' +
      'Qualification must publish one canonical pass/fail result marker ' +
      'for issue ' + qualificationNumber + ' at this exact SHA ' +
      '(continuum-qualification-result with issue, sha, and result pass/fail). ' +
      'Prose verdicts never count; stale SHAs never satisfy the gate. ' +
      'Run identity is immutable: capability=' + capabilityNumber +
      ' qualification=' + qualificationNumber + ' sha=' + sha + '.';
    if (opencodeDispatch === 'workflow') {
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: qualificationNumber,
        body: [marker, dispatchMarker, '⚡ **Continuum · issue-scheduler**', '<!-- continuum-origin role=continuum component=issue-scheduler -->', '', note].join('\n'),
      });
      await github.request(
        'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches',
        {
          owner,
          repo,
          workflow_id: opencodeWorkflowPath.replace(
            /^\.github\/workflows\//,
            ''
          ),
          ref: dispatchRef,
          inputs: {
            mode: 'qualification',
            issue_number: String(qualificationNumber),
            capability_number: String(capabilityNumber),
            qualification_number: String(qualificationNumber),
            required_sha: sha,
          },
        }
      );
    } else {
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: qualificationNumber,
        body: ['/oc', '', marker, dispatchMarker, '⚡ **Continuum · issue-scheduler**', '<!-- continuum-origin role=continuum component=issue-scheduler -->', '', note].join('\n'),
      });
    }
    await addLabel(qualificationNumber, inProgressLabel);
    return wasPaused ? 'unpause-and-dispatch' : 'dispatch';
  }
  
  // Reconcile one capability through the qualification phase.
  // Returns the lifecycle state: 'complete', 'qualifying', or
  // 'blocked'. A closed capability without exact-SHA pass evidence
  // fails closed ('must-reopen'), so auto-close keywords can never
  // bypass the gate.
  async function reconcileQualificationCapability(capabilityIssue) {
    const capabilityNumber = capabilityIssue.number;
    rememberIssue(capabilityIssue);
    const refs = qualificationRefs(
      capabilityIssue.body || '', capabilityNumber
    );
    if (refs.length === 0) return 'no-qualification';
    let requiredSha = latestRequiredSha(
      capabilityIssue.body || '', capabilityNumber
    );
    if (!requiredSha) {
      // A bare qualification relation never starts qualification
      // and never synthesizes a required SHA from current main.
      // Only the trusted implementation-merge transition records
      // the first SHA. Repair/qualification issues are never
      // created before that transition, and open blockers keep
      // the capability in its normal pre-merge state.
      core.notice(
        'Capability #' + capabilityNumber +
        ': qualification declared but implementation has not merged; waiting for the merge transition.'
      );
      return 'no-required-sha';
    }
    // Qualification cannot leapfrog open blockers/unimplemented
    // capability work: they remain blockers until closed. A direct
    // capability<->qualification back-edge is the one exception
    // (kodmial/continuum#278): the capability intentionally stays
    // open while it qualifies, so its own validator is ignored
    // with a diagnostic instead of stranding qualification.
    const declaredBlockers = await openDeclaredBlockers(capabilityIssue);
    const qualifierSplit = effectiveQualificationBlockers(
      refs, declaredBlockers.map(blocker => blocker.number)
    );
    if (qualifierSplit.ignored.length > 0) {
      core.notice(
        'Capability #' + capabilityNumber +
        ': ignoring qualification back-edge blocker(s) ' +
        qualifierSplit.ignored.map(number => '#' + number).join(', ') +
        ' for qualification readiness.'
      );
    }
    if (qualifierSplit.effective.length > 0) {
      core.info(
        'Capability #' + capabilityNumber + ': blocked by ' +
        qualifierSplit.effective.map(number => '#' + number).join(', ') +
        '; qualification waits.'
      );
      return 'blocked-waiting';
    }
    try {
      const nativeBlockers = await snapshotNativeBlockersPat(capabilityNumber);
      if (nativeBlockers.some(blocker => blocker.state === 'open')) {
        core.info(
          'Capability #' + capabilityNumber +
          ': native blocker open; qualification waits.'
        );
        return 'blocked-waiting';
      }
    } catch (err) {
      if (err.status !== 404) throw err;
    }
    const mainSha = await currentMainSha();
    if (normalizeSha(mainSha) !== requiredSha) {
      // A repair merge advanced main: stale evidence no longer
      // matches, so record the new exact SHA and rerun.
      requiredSha = normalizeSha(mainSha);
      await github.rest.issues.update({
        owner,
        repo,
        issue_number: capabilityNumber,
        body: (capabilityIssue.body || '') +
          '\n' + requiredShaMarker(capabilityNumber, requiredSha),
      });
      capabilityIssue.body =
        (capabilityIssue.body || '') +
        '\n' + requiredShaMarker(capabilityNumber, requiredSha);
      core.notice(
        'Capability #' + capabilityNumber +
        ': main advanced; qualification reruns for ' + requiredSha + '.'
      );
    }
    const evidence = {};
    const evidenceBodies = {};
    for (const ref of refs) {
      let bodies = [];
      try {
        bodies = await issueCommentsBodies(ref);
      } catch (err) {
        if (err.status !== 404) throw err;
      }
      evidenceBodies[ref] = bodies;
      evidence[ref] = qualificationEvidenceState(
        bodies, ref, requiredSha
      );
    }
    const states = refs.map(ref => evidence[ref]);
    const failures = refs.filter(ref => evidence[ref] === 'fail');
    if (states.every(state => state === 'pass')) {
      await removeLabel(capabilityNumber, inProgressLabel);
      await removeLabel(capabilityNumber, pausedLabel);
      await removeLabel(capabilityNumber, qualifyingLabel);
      await removeLabel(capabilityNumber, blockedLabel);
      if (capabilityIssue.state !== 'closed') {
        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: capabilityNumber,
          body: [
            '⚡ **Continuum · issue-scheduler**',
            '<!-- continuum-origin role=continuum component=issue-scheduler -->',
            'Capability complete: mandatory qualification passed for exact main `' +
              requiredSha + '` (' +
              refs.map(ref => '#' + ref).join(', ') + ').',
          ].join('\n'),
        });
        await github.rest.issues.update({
          owner,
          repo,
          issue_number: capabilityNumber,
          state: 'closed',
          state_reason: 'completed',
        });
        updateOpenSnapshot({ ...capabilityIssue, state: 'closed' });
        invalidateNativeBlockers();
      }
      core.notice(
        'Capability #' + capabilityNumber +
        ' completed with exact-SHA qualification evidence.'
      );
      return 'complete';
    }
    await addLabel(capabilityNumber, qualifyingLabel);
    if (failures.length > 0) {
      await addLabel(capabilityNumber, blockedLabel);
      await ensureRepairIssue(capabilityNumber, failures, requiredSha, evidenceBodies);
      for (const ref of refs) {
        await dispatchQualificationIssue(
          capabilityNumber, ref, requiredSha
        );
      }
      core.warning(
        'Capability #' + capabilityNumber +
        ' blocked: qualification failure in ' +
        failures.map(ref => '#' + ref).join(', ') + '.'
      );
      return 'blocked';
    }
    await removeLabel(capabilityNumber, blockedLabel);
    for (const ref of refs) {
      await dispatchQualificationIssue(
        capabilityNumber, ref, requiredSha
      );
    }
    return 'qualifying';
  }
  
  // A failed qualification routes repair work back into the
  // capability lifecycle as a tracked issue. At most one open
  // repair issue exists per (capability, qualification) pair, so
  // repeated failures cannot file repair storms.
  const repairMarkerPattern =
    /<!--\s*continuum-qualification-repair\s+source\s*=\s*(\d+)\s+capability\s*=\s*(\d+)\s*-->/i;
  
  async function ensureRepairIssue(
    capabilityNumber,
    failures,
    requiredSha,
    evidenceBodiesByRef
  ) {
    // Pass-level open-issue inventory (Work-Lock #58 item 6 A5):
    // reuse the scheduler's snapshot when available instead of a
    // duplicate full open-issue pagination. The inventory is
    // updated below whenever this run creates or refreshes a
    // repair issue so later decisions cannot use knowingly stale
    // state; a run without a snapshot falls back to one lazy
    // paginated fetch that then seeds the snapshot.
    let openIssues;
    if (openSnapshotByNumber) {
      openIssues = [...openSnapshotByNumber.values()];
    } else {
      openIssues = await readGithub.paginate(
        readGithub.rest.issues.listForRepo,
        { owner, repo, state: 'open', per_page: 100 }
      );
      rememberOpenSnapshot(openIssues);
    }
    for (const failure of failures) {
      const bodies = (evidenceBodiesByRef && evidenceBodiesByRef[failure]) || [];
      const latestEvidence = latestTrustedEvidenceBody(bodies, failure, requiredSha) || '';
      const handoffLines = [
        '<!-- continuum-qualification-repair source=' + failure +
        ' capability=' + capabilityNumber + ' -->',
        '',
        'Mandatory qualification #' + failure + ' for capability #' +
          capabilityNumber + ' failed at main `' + requiredSha + '`.',
        '',
        'Failure handoff (read before editing code):',
        '- qualification issue: #' + failure,
        '- capability issue: #' + capabilityNumber,
        '- required SHA: `' + requiredSha + '`',
      ];
      if (latestEvidence) {
        handoffLines.push(
          '',
          'Latest trusted failure evidence for this exact SHA:',
          '',
          latestEvidence
        );
      }
      handoffLines.push(
        '',
        'Fix the concrete failing scenarios from the evidence above; do not',
        'guess from the generic title alone. Merge the fix and the',
        'qualification gate automatically reruns qualification #' +
          failure + ' against the new exact main SHA.',
        'Do not close capability #' + capabilityNumber +
          ' manually: only exact-SHA pass evidence may complete it.'
      );
      const handoffBody = handoffLines.join('\n');
      const existing = openIssues.find(
        item =>
          !item.pull_request &&
          (item.body || '').match(repairMarkerPattern) &&
          Number((item.body || '').match(repairMarkerPattern)[1]) === failure &&
          Number((item.body || '').match(repairMarkerPattern)[2]) === capabilityNumber
      );
      if (existing) {
        const current = existing.body || '';
        const stale = current.indexOf(requiredSha) === -1 ||
          (latestEvidence && current.indexOf(latestEvidence) === -1);
        if (stale) {
          await github.rest.issues.update({
            owner,
            repo,
            issue_number: existing.number,
            body: handoffBody,
          });
          updateOpenSnapshot({ ...existing, body: handoffBody, state: 'open' });
          core.warning(
            'Refreshed repair issue #' + existing.number +
            ' with latest exact-SHA failure evidence for qualification #' + failure + '.'
          );
        } else {
          core.info(
            'Repair for qualification #' + failure +
            ' already tracked in #' + existing.number + '.'
          );
        }
        continue;
      }
      const created = await github.rest.issues.create({
        owner,
        repo,
        title: 'Repair qualification failure for #' + capabilityNumber +
          ' (qualification #' + failure + ')',
        body: handoffBody,
        labels: ['priority:p0'],
      });
      updateOpenSnapshot({ ...created.data, body: handoffBody, state: 'open' });
      invalidateNativeBlockers();
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: capabilityNumber,
        body: [
          '⚡ **Continuum · issue-scheduler**',
          '<!-- continuum-origin role=continuum component=issue-scheduler -->',
          'Qualification #' + failure + ' failed at main `' +
            requiredSha + '`; repair tracked in #' + created.data.number + '.',
        ].join('\n'),
      });
      core.warning(
        'Created repair issue #' + created.data.number +
        ' for qualification #' + failure + '.'
      );
    }
  }
  
  // An implementation merge enters the qualifying state instead of
  // completing the capability. Returns 'missing' when the issue is
  // gone, otherwise the declared qualification refs (empty means
  // the caller runs the normal completion path).
  async function enterQualifyingState(
    issueNumber,
    mergedSha,
    originText
  ) {
    let fresh;
    try {
      fresh = (await readGithub.rest.issues.get({
        owner,
        repo,
        issue_number: issueNumber,
      })).data;
    } catch (err) {
      if (err.status === 404) return 'missing';
      throw err;
    }
    if (fresh.pull_request) return 'missing';
    rememberIssue(fresh);
    const refs = qualificationRefs(fresh.body || '', issueNumber);
    if (refs.length === 0) return refs;
    // Qualification cannot leapfrog open blockers: the merge still
    // records nothing and dispatches nothing until they close. A
    // direct back-edge on the capability's own validator is
    // ignored with a diagnostic (kodmial/continuum#278).
    const mergeDeclaredBlockers = await openDeclaredBlockers(fresh);
    const mergeSplit = effectiveQualificationBlockers(
      refs, mergeDeclaredBlockers.map(blocker => blocker.number)
    );
    if (mergeSplit.ignored.length > 0) {
      core.notice(
        'Capability #' + issueNumber +
        ': ignoring qualification back-edge blocker(s) ' +
        mergeSplit.ignored.map(number => '#' + number).join(', ') +
        ' at the merge transition.'
      );
    }
    if (mergeSplit.effective.length > 0) {
      core.info(
        'Capability #' + issueNumber + ': implementation merged but blocked by ' +
        mergeSplit.effective.map(number => '#' + number).join(', ') +
        '; qualification waits for blockers.'
      );
      return 'blocked-waiting';
    }
    const sha = normalizeSha(mergedSha) || (await currentMainSha());
    const knownSha = latestRequiredSha(fresh.body || '', issueNumber);
    if (knownSha !== sha) {
      await github.rest.issues.update({
        owner,
        repo,
        issue_number: issueNumber,
        body: (fresh.body || '') + '\n' + requiredShaMarker(issueNumber, sha),
      });
    }
    if (fresh.state === 'closed') {
      await github.rest.issues.update({
        owner,
        repo,
        issue_number: issueNumber,
        state: 'open',
      });
      updateOpenSnapshot({ ...fresh, state: 'open' });
      invalidateNativeBlockers();
      core.warning(
        'Capability #' + issueNumber +
        ' closed with pending mandatory qualification (' + originText +
        '); reopened instead of completing (fail closed).'
      );
    }
    await removeLabel(issueNumber, inProgressLabel);
    await removeLabel(issueNumber, pausedLabel);
    await removeLabel(issueNumber, blockedLabel);
    await addLabel(issueNumber, qualifyingLabel);
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: issueNumber,
      body: [
        '⚡ **Continuum · issue-scheduler**',
        '<!-- continuum-origin role=continuum component=issue-scheduler -->',
        'Implementation merged (' + originText + ') at main `' + sha + '`.',
        '',
        'Capability #' + issueNumber + ' enters `' + qualifyingLabel +
          '`: mandatory qualification (' +
          refs.map(ref => '#' + ref).join(', ') +
          ') must publish exact-SHA pass evidence before completion.',
        'Implementation completion and capability completion are distinct states.',
      ].join('\n'),
    });
    for (const ref of refs) {
      const outcome = await dispatchQualificationIssue(
        issueNumber,
        ref,
        sha
      );
      core.notice(
        'Qualification #' + ref + ' for capability #' +
        issueNumber + ': ' + outcome + '.'
      );
    }
    return refs;
  }
  
  // Every dispatch in this job goes through the PAT, so no
  // `actions: write` grant is needed on the caller's token.
  async function dispatchWorkflow(workflow, inputs) {
    await github.request(
      'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches',
      { owner, repo, workflow_id: workflow, ref: dispatchRef, inputs }
    );
  }
  
  // Label specs mirror the configured names the reconcile pass
  // below uses. Keeping them in one builder preserves newly
  // installed repositories, optional execution routes and the
  // optional admission label without creating duplicates.
  function buildSchedulerLabelSpecs(config) {
    const settings = config || {};
    const inProgressLabel =
      settings.inProgressLabel || 'automation:in-progress';
    const pausedLabel =
      settings.pausedLabel || 'automation:paused';
    const qualifyingLabel =
      settings.qualifyingLabel || 'automation:qualifying';
    const blockedLabel =
      settings.blockedLabel || 'automation:blocked';
    const executionRouteLabels = settings.executionRouteLabels || [];
    const readyLabel = settings.readyLabel || '';
    return [
      {
        name: 'priority:p0',
        color: 'B60205',
        description: 'P0 — critical work that should be handled immediately.',
      },
      {
        name: 'priority:p1',
        color: 'D93F0B',
        description: 'P1 — high-priority planned work.',
      },
      {
        name: 'priority:p2',
        color: 'FBCA04',
        description: 'P2 — normal planned work.',
      },
      {
        name: inProgressLabel,
        color: '5319E7',
        description: 'Actively reserved or implemented by the automatic issue scheduler.',
      },
      {
        name: pausedLabel,
        color: 'BFDADC',
        description: 'Automation is paused after a failed/stopped implementation; remove to retry.',
      },
      {
        name: qualifyingLabel,
        color: '0E8A16',
        description: 'Implementation merged; mandatory live qualification is pending exact-SHA evidence.',
      },
      {
        name: blockedLabel,
        color: 'B60205',
        description: 'Mandatory live qualification failed; repair is required before completion.',
      },
      // A configured route label is reserved before this run can see
      // it, so create it here rather than letting a consumer's
      // selection silently no-op on a missing label.
      ...[...executionRouteLabels].map(name => ({
        name,
        color: '0E8A16',
        description:
          'Routes this issue to a configured execution workflow instead of the OpenCode agent.',
      })),
      // The optional admission label is created here for the same
      // reason: a consumer can apply it from the issue page without
      // a missing-label selection silently no-op. Empty creates
      // nothing and keeps current behavior unchanged.
      ...(readyLabel ? [{
        name: readyLabel,
        color: '0E8A16',
        description:
          'Admits this issue to automatic implementation by the issue scheduler.',
      }] : []),
    ];
  }
  
  // Provision labels from a complete repository inventory instead of
  // sending one POST per spec on every pass (kodmial/continuum#388).
  // The same-repository GITHUB_TOKEN reads the inventory, so a
  // steady-state pass spends no shared-PAT write and no
  // already-existing label is re-POSTed. Pagination is authoritative:
  // a repository with more than 100 labels must never hide a
  // required one.
  //
  // Any inventory failure aborts the pass before reconciliation.
  // Treating a 403/429/5xx/timeout/permission failure as an empty
  // repository would fan out creates on an exhausted budget and could
  // authorize implementation from an uncertain label state.
  async function provisionSchedulerLabels({
    readClient,
    writeClient,
    repositoryOwner,
    repositoryName,
    specs,
  }) {
    let inventory;
    try {
      inventory = await readClient.paginate(
        readClient.rest.issues.listLabelsForRepo,
        {
          owner: repositoryOwner,
          repo: repositoryName,
          per_page: 100,
        }
      );
    } catch (err) {
      const status =
        err && err.status ? ' (status=' + err.status + ')' : '';
      throw new Error(
        'Label inventory read failed' + status +
          '; refusing to provision labels or reconcile this pass.'
      );
    }
    if (!Array.isArray(inventory)) {
      throw new Error(
        'Label inventory read returned an invalid page; refusing to provision labels or reconcile this pass.'
      );
    }
    const existingNames = new Set();
    for (const label of inventory) {
      if (
        !label ||
        typeof label.name !== 'string' ||
        !label.name
      ) {
        throw new Error(
          'Label inventory contained an invalid entry; refusing to provision labels or reconcile this pass.'
        );
      }
      existingNames.add(label.name);
    }
    const attempted = new Set();
    for (const spec of specs) {
      if (!spec || typeof spec.name !== 'string' || !spec.name) {
        throw new Error(
          'Label specification is invalid; refusing to provision labels or reconcile this pass.'
        );
      }
      if (attempted.has(spec.name)) continue;
      attempted.add(spec.name);
      if (existingNames.has(spec.name)) continue;
      try {
        await writeClient.rest.issues.createLabel({
          owner: repositoryOwner,
          repo: repositoryName,
          ...spec,
        });
      } catch (err) {
        // A concurrent actor can create the label between the
        // inventory read and this POST. 422 is the existing-label
        // race, so it stays idempotent; everything else aborts.
        if (err && err.status === 422) {
          existingNames.add(spec.name);
          continue;
        }
        const status =
          err && err.status ? ' (status=' + err.status + ')' : '';
        throw new Error(
          'Label provisioning failed' + status +
            '; refusing to reconcile this pass. Retry after the next event or cron wake.'
        );
      }
      existingNames.add(spec.name);
    }
  }
  
  const labelSpecs = buildSchedulerLabelSpecs({
    inProgressLabel,
    pausedLabel,
    qualifyingLabel,
    blockedLabel,
    executionRouteLabels,
    readyLabel,
  });
  
  await provisionSchedulerLabels({
    readClient: readGithub,
    writeClient: github,
    repositoryOwner: owner,
    repositoryName: repo,
    specs: labelSpecs,
  });
  
  const labelsByIssue = new Map();
  
  function rememberIssue(issue) {
    labelsByIssue.set(
      issue.number,
      new Set(
        (issue.labels || []).map(label =>
          typeof label === 'string' ? label : label.name
        )
      )
    );
  }
  
  async function addLabel(issueNumber, name) {
    const labels = labelsByIssue.get(issueNumber);
    if (labels?.has(name)) return;
    await github.rest.issues.addLabels({
      owner,
      repo,
      issue_number: issueNumber,
      labels: [name],
    });
    if (labels) labels.add(name);
  }
  
  async function removeLabel(issueNumber, name) {
    const labels = labelsByIssue.get(issueNumber);
    if (labels && !labels.has(name)) return;
    try {
      await github.rest.issues.removeLabel({
        owner,
        repo,
        issue_number: issueNumber,
        name,
      });
    } catch (err) {
      if (err.status !== 404) throw err;
    }
    if (labels) labels.delete(name);
  }
  
  async function pauseIssue(issueNumber, reason) {
    await removeLabel(issueNumber, inProgressLabel);
    if (!pauseOnFailure) {
      await removeLabel(issueNumber, pausedLabel);
      core.warning(
        'Released issue #' + issueNumber +
        ' for automatic retry instead of pausing: ' + reason
      );
      return;
    }
    await addLabel(issueNumber, pausedLabel);
    core.warning('Paused issue #' + issueNumber + ': ' + reason);
    if (postPauseComment) {
      try {
        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: issueNumber,
          body: [
            '⚡ **Continuum · issue-scheduler**',
            '<!-- continuum-origin role=continuum component=issue-scheduler -->',
            'Automation paused: ' + reason + '.',
            '',
            'Remove the `' + pausedLabel + '` label to retry.',
          ].join('\n'),
        });
      } catch (err) {
        core.warning(
          'Could not post the pause comment for #' +
            issueNumber +
            ': ' +
            err.message
        );
      }
    }
  }
  
  // A closed OpenCode PR is authoritative:
  // - merged means the implementation is done -> close the issue;
  // - closed without merge means the attempt was rejected/stopped ->
  //   pause automation instead of looping and reopening PRs forever.
  if (context.eventName === 'pull_request_target') {
    const pr = context.payload.pull_request;
    if (pr?.head?.repo?.full_name === repoFullName) {
      const match = (pr.head.ref || '').match(/^opencode\/issue(\d+)-/);
      if (match) {
        const issueNumber = Number(match[1]);
        try {
          const response = await readGithub.rest.issues.get({
            owner,
            repo,
            issue_number: issueNumber,
          });
          rememberIssue(response.data);
  
          if (pr.merged) {
            const entered = await enterQualifyingState(
              issueNumber,
              pr.merge_commit_sha,
              'merged PR #' + pr.number
            );
            if (entered === 'blocked-waiting') {
              core.notice(
                'Merged OpenCode PR #' + pr.number +
                ': capability #' + issueNumber +
                ' stays blocked; qualification waits for blockers.'
              );
            } else if (entered === 'missing' || entered.length === 0) {
              await removeLabel(issueNumber, inProgressLabel);
              await removeLabel(issueNumber, pausedLabel);
              if (response.data.state !== 'closed') {
                await github.rest.issues.update({
                  owner,
                  repo,
                  issue_number: issueNumber,
                  state: 'closed',
                  state_reason: 'completed',
                });
                updateOpenSnapshot({ number: issueNumber, state: 'closed' });
                invalidateNativeBlockers();
              }
              core.notice(
                'Merged OpenCode PR #' + pr.number +
                ' completed issue #' + issueNumber + '.'
              );
            } else {
              core.notice(
                'Merged OpenCode PR #' + pr.number +
                ': capability #' + issueNumber +
                ' enters qualification instead of completing.'
              );
            }
          } else {
            const blockers = await snapshotNativeBlockers(issueNumber);
            const openBlockers = blockers.filter(
              blocker => blocker.state === 'open'
            );
  
            // A rejected run against a blocked issue is not a
            // failure of the implementation; it is work that was not
            // eligible yet. Release the reservation and let the
            // dependency, not the pause label, decide readiness.
            if (openBlockers.length > 0) {
              await removeLabel(issueNumber, inProgressLabel);
              await removeLabel(issueNumber, pausedLabel);
              core.notice(
                'OpenCode PR #' + pr.number +
                ' closed without merge for blocked issue #' +
                issueNumber + '; reservation released, issue remains ' +
                'eligible after blockers complete.'
              );
            } else {
              await pauseIssue(
                issueNumber,
                'OpenCode PR #' + pr.number +
                ' was closed without merge.'
              );
            }
          }
        } catch (err) {
          if (err.status !== 404) throw err;
        }
      }
    }
  }
  
  // A closed issue is terminal for scheduler admission and must not
  // retain an implementation reservation. Reconcile this in the
  // shared engine rather than in a consumer caller so every consumer
  // releases stale WIP immediately after an issue is closed.
  const closedLeasedIssues = await readGithub.paginate(
    readGithub.rest.issues.listForRepo,
    {
      owner,
      repo,
      state: 'closed',
      labels: inProgressLabel,
      per_page: 100,
    }
  );
  for (const issue of closedLeasedIssues) {
    if (issue.pull_request) continue;
    await removeLabel(issue.number, inProgressLabel);
    core.notice(
      'Released stale reservation on closed issue #' +
        issue.number +
        '.'
    );
  }
  
  let issues = await readGithub.paginate(readGithub.rest.issues.listForRepo, {
    owner,
    repo,
    state: 'open',
    per_page: 100,
  });
  // Seed the pass-level open-issue inventory before filtering so
  // declared-blocker admission sees the complete open set (an
  // absent number is not an open local blocker, present is open).
  rememberOpenSnapshot(issues);
  issues = issues.filter(issue => !issue.pull_request);
  for (const issue of issues) rememberIssue(issue);
  
  if (!pauseOnFailure) {
    for (const issue of issues) {
      if (labelsByIssue.get(issue.number).has(pausedLabel)) {
        await removeLabel(issue.number, pausedLabel);
        core.notice(
          'Cleared legacy automation pause on issue #' +
          issue.number +
          ' because pause_on_failure=false.'
        );
      }
    }
  }
  
  // Fail-closed auto-close sweep. A PR body carrying `Fixes #N`
  // auto-closes the capability at merge time, bypassing the merge
  // handler above when the event is missed or the label was never
  // applied. The latest closed issues are bounded (one page), so
  // this sweep costs one API call plus follow-ups only for
  // capabilities that declare mandatory qualification.
  const { data: recentlyClosed } = await readGithub.rest.issues.listForRepo({
    owner,
    repo,
    state: 'closed',
    sort: 'updated',
    direction: 'desc',
    per_page: 30,
  });
  for (const closed of recentlyClosed) {
    if (closed.pull_request) continue;
    const refs = qualificationRefs(closed.body || '', closed.number);
    if (refs.length === 0) continue;
    rememberIssue(closed);
    const requiredSha = latestRequiredSha(
      closed.body || '',
      closed.number
    );
    const commentBodies = {};
    for (const ref of refs) {
      try {
        commentBodies[ref] = await issueCommentsBodies(ref);
      } catch (err) {
        if (err.status === 404) commentBodies[ref] = [];
        else throw err;
      }
    }
    const complete = requiredSha &&
      refs.every(
        ref =>
          qualificationEvidenceState(
            commentBodies[ref],
            ref,
            requiredSha
          ) === 'pass'
      );
    if (complete) {
      await removeLabel(closed.number, qualifyingLabel);
      await removeLabel(closed.number, blockedLabel);
      core.notice(
        'Closed capability #' + closed.number +
        ' holds exact-SHA qualification evidence; no reopen needed.'
      );
      continue;
    }
    await github.rest.issues.update({
      owner,
      repo,
      issue_number: closed.number,
      state: 'open',
    });
    await addLabel(closed.number, qualifyingLabel);
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: closed.number,
      body: [
        '⚡ **Continuum · issue-scheduler**',
        '<!-- continuum-origin role=continuum component=issue-scheduler -->',
        'Reopened by the qualification gate: this capability declares mandatory qualification (' +
          refs.map(ref => '#' + ref).join(', ') +
          ') but holds no exact-SHA pass evidence' +
          (requiredSha ? ' for main `' + requiredSha + '`.' : '.') +
          ' Auto-close keywords cannot bypass qualification.',
      ].join('\n'),
    });
    core.warning(
      'Reopened auto-closed capability #' + closed.number +
      ' with pending mandatory qualification.'
    );
    issues.push({ ...closed, state: 'open' });
    updateOpenSnapshot({ ...closed, state: 'open' });
    invalidateNativeBlockers();
  }
  
  // Open-capability reconcile: exact-SHA evidence may complete the
  // capability, failure blocks it with a repair issue, and anything
  // else keeps it qualifying with its qualifications dispatched.
  // A main advance since the recorded SHA invalidates stale
  // evidence and reruns qualification automatically.
  for (const issue of issues) {
    const refs = qualificationRefs(issue.body || '', issue.number);
    if (refs.length === 0) continue;
    const outcome = await reconcileQualificationCapability(issue);
    core.notice(
      'Capability #' + issue.number +
      ' qualification reconcile: ' + outcome + '.'
    );
  }
  
  // Manual owner-triggered /oc runs are real in-flight work too.
  // Reserve the issue immediately so the scheduler cannot enqueue a
  // second OpenCode run before the first one creates its PR.
  if (context.eventName === 'issue_comment') {
    const eventIssue = context.payload.issue;
    const comment = context.payload.comment;
    const body = comment?.body || '';
    const isOwnerCommand =
      comment?.user?.login === owner &&
      classifyComments([body])[0] === 'run';
  
    if (
      eventIssue &&
      !eventIssue.pull_request &&
      eventIssue.state === 'open' &&
      isOwnerCommand
    ) {
      if (!labelsByIssue.has(eventIssue.number)) {
        rememberIssue(eventIssue);
      }
      await addLabel(eventIssue.number, inProgressLabel);
      core.notice(
        'Reserved issue #' + eventIssue.number +
        ' for manually triggered OpenCode work.'
      );
    }
  }
  
  // One-time migration for the current backlog only. After this,
  // priority labels are the source of truth. No default priority is
  // assigned: an unprioritized issue is valid and sorts after P2.
  for (const issue of issues) {
    const labels = labelsByIssue.get(issue.number);
    const currentPriorities = [...labels].filter(name =>
      priorityRank.has(name)
    );
    if (currentPriorities.length !== 0) continue;
  
    const match = issue.title.match(/^P([0-2]):\s+/i);
    if (!match) continue;
  
    const priority = ('priority:p' + match[1]).toLowerCase();
    const cleanTitle = issue.title.replace(/^P[0-2]:\s+/i, '');
  
    await addLabel(issue.number, priority);
    if (cleanTitle !== issue.title) {
      await github.rest.issues.update({
        owner,
        repo,
        issue_number: issue.number,
        title: cleanTitle,
      });
      issue.title = cleanTitle;
    }
  
    core.notice(
      'Migrated issue #' + issue.number + ' to ' + priority + '.'
    );
  }
  
  // Read every OpenCode issue PR, not just open ones. This lets the
  // cron run reconcile missed PR-close events from current repository
  // state rather than depending on a permanent dispatch marker.
  const pulls = await readGithub.paginate(readGithub.rest.pulls.list, {
    owner,
    repo,
    state: 'all',
    per_page: 100,
  });
  
  const openPrIssues = new Set();
  const latestPrByIssue = new Map();
  
  for (const pr of pulls) {
    if (pr.head?.repo?.full_name !== repoFullName) continue;
    const match = (pr.head.ref || '').match(/^opencode\/issue(\d+)-/);
    if (!match) continue;
  
    const issueNumber = Number(match[1]);
    const previous = latestPrByIssue.get(issueNumber);
    if (!previous || pr.number > previous.number) {
      latestPrByIssue.set(issueNumber, pr);
    }
    if (pr.state === 'open') openPrIssues.add(issueNumber);
  }
  
  const issueByNumber = new Map(issues.map(issue => [issue.number, issue]));
  const issueNumbersByTitle = new Map();
  for (const issue of issues) {
    if (!issueNumbersByTitle.has(issue.title)) {
      issueNumbersByTitle.set(issue.title, []);
    }
    issueNumbersByTitle.get(issue.title).push(issue.number);
  }
  
  const activeIssueNumbers = new Set();
  // Verified 429 checkpoints awaiting infrastructure recovery keep
  // their reservation label for watchdog discovery but must never
  // consume a coding WIP slot or be restarted from main.
  const recoveryPendingIssueNumbers = new Set();
  
  // Active/pending OpenCode Actions are authoritative in-flight work.
  // Without this, lease expiry releases an issue while GitHub is still
  // running or queueing its implementation.
  // The scheduler only needs recent active work. Never paginate the
  // complete Actions history: large repositories can exhaust the
  // GitHub API quota during a single reconciliation pass.
  const workflowRuns = [];
  for (let page = 1; page <= 3; page += 1) {
    const { data } =
      await readGithub.rest.actions.listWorkflowRunsForRepo({
        owner,
        repo,
        per_page: 100,
        page,
      });
    workflowRuns.push(...data.workflow_runs);
    if (data.workflow_runs.length < 100) break;
  }
  
  const activeRunIssues = new Set();
  for (const run of workflowRuns) {
    const isOpenCodeWorkflow =
      run.path === opencodeWorkflowPath ||
      run.name === opencodeWorkflowName;
    // kodmial/continuum#350: a fresh-VM 429 resumption is a
    // workflow_dispatch run. It must count as authoritative
    // in-flight work exactly like an issue_comment run, or lease
    // expiry would re-dispatch the task from scratch while the
    // recovery runner is still starting.
    if (
      !isOpenCodeWorkflow ||
      run.status === 'completed' ||
      (run.event !== 'issue_comment' &&
       run.event !== 'workflow_dispatch')
    ) {
      continue;
    }
  
    const numbered = (run.display_title || '').match(
      /^OpenCode issue #(\d+)$/
    );
    if (numbered) {
      activeRunIssues.add(Number(numbered[1]));
      continue;
    }
  
    // A caller without `run-name:` reports the issue title instead,
    // which is only unambiguous while exactly one issue carries it.
    const matches = issueNumbersByTitle.get(run.display_title || '') || [];
    if (matches.length === 1) {
      activeRunIssues.add(matches[0]);
    }
  }
  
  for (const issueNumber of activeRunIssues) {
    const activeIssue = issueByNumber.get(issueNumber);
    if (!activeIssue) continue;
    const activeLabels = labelsByIssue.get(issueNumber) || new Set();
    // With explicit admission enabled, implementation WIP counts only
    // admitted coding work. Qualification/control/manual runs may
    // execute concurrently but must not consume automatic coding
    // capacity.
    if (!readyLabel || activeLabels.has(readyLabel)) {
      activeIssueNumbers.add(issueNumber);
    }
    await addLabel(issueNumber, inProgressLabel);
    await removeLabel(issueNumber, pausedLabel);
  }
  
  // An open OpenCode PR is still visible work, but consumers may
  // choose not to spend an implementation WIP slot while that PR is
  // merely waiting for CI, review, hardware, or merge.
  for (const issueNumber of openPrIssues) {
    if (countOpenPrsAsWip) {
      activeIssueNumbers.add(issueNumber);
    }
    if (issueByNumber.has(issueNumber)) {
      await addLabel(issueNumber, inProgressLabel);
      await removeLabel(issueNumber, pausedLabel);
    }
  }
  
  // Durable 429 recovery state (kodmial/continuum#418). The recent
  // Actions listing is a wake-up hint, never proof of ownership. An
  // exact run GET plus the durable checkpoint metadata decides
  // whether a marker still owns work, so a stale `observed` marker
  // cannot hold a WIP slot for 48 hours and a verified checkpoint
  // cannot be restarted from main just because its run aged out.
  function parseSchedulerRecoveryMarkers(body) {
    const re = /<!--\s*continuum-429-recovery\s+op=(\S+)\s+gen=(\d+)\s+state=(\S+)\s+attempt=(\d+)\s+next_due=(\d+)\s+checkpoint=(\S*)\s+sha=(\S*)\s+run=(\S*)(?:\s+detail=(\S*))?\s*-->/g;
    const text = String(body || '');
    const found = [];
    let match;
    while ((match = re.exec(text)) !== null) {
      found.push({
        operation: match[1],
        generation: Number(match[2]),
        state: match[3],
        attempt: Number(match[4]),
        nextDue: Number(match[5]),
        checkpointRef: match[6] === '-' ? '' : match[6],
        checkpointSha: match[7] === '-' ? '' : match[7],
        runId: match[8] === '-' ? '' : match[8],
        detail: !match[9] || match[9] === '-' ? '' : match[9],
      });
    }
    return found;
  }
  
  // Only the workflow actor may publish recovery state; a forged
  // comment from any other account is ignored.
  function trustedSchedulerRecoveryComment(comment) {
    const login = String(comment.user?.login || '').toLowerCase();
    if (login === 'github-actions[bot]') return true;
    return login === String(owner || '').toLowerCase();
  }
  
  function latestSchedulerRecoveryState(comments) {
    let latest = null;
    const ordered = comments.slice().sort(
      (a, b) => Number(a.id || 0) - Number(b.id || 0)
    );
    for (const comment of ordered) {
      if (!trustedSchedulerRecoveryComment(comment)) continue;
      const markers = parseSchedulerRecoveryMarkers(
        comment.body || ''
      );
      for (let index = 0; index < markers.length; index += 1) {
        const candidate = {
          ...markers[index],
          commentId: Number(comment.id || 0),
          markerIndex: index,
          commentAt: new Date(
            comment.updated_at || comment.created_at || 0
          ).getTime(),
        };
        if (
          !latest ||
          candidate.commentId > latest.commentId ||
          (candidate.commentId === latest.commentId &&
            candidate.markerIndex >= latest.markerIndex)
        ) {
          latest = candidate;
        }
      }
    }
    return latest;
  }
  
  function parseSchedulerCheckpointIdentity(checkpointRef) {
    const match = /^opencode\/(?:429|infra)-checkpoint-(.+)-gen-(\d+)$/.exec(
      String(checkpointRef || '')
    );
    if (!match) return null;
    return {
      operation: match[1],
      generation: Number(match[2]),
    };
  }
  
  async function fetchSchedulerRecoveryRun(runId) {
    const text = String(runId || '').trim();
    if (!/^\d+$/.test(text)) return { kind: 'none' };
    const recent = workflowRuns.find(
      run => String(run.id) === text
    );
    if (recent) {
      return { kind: 'found', run: recent, source: 'recent' };
    }
    try {
      const { data } =
        await readGithub.rest.actions.getWorkflowRun({
          owner, repo, run_id: Number(text),
        });
      if (!data || typeof data !== 'object') {
        return { kind: 'unavailable', status: 0 };
      }
      return { kind: 'found', run: data, source: 'exact' };
    } catch (err) {
      const status = Number(err && err.status || 0);
      if (status === 404) return { kind: 'missing', status };
      return { kind: 'unavailable', status };
    }
  }
  
  async function readSchedulerCheckpointEvidence({
    operation, generation, checkpointRef, issueNumber,
  }) {
    let refSha = '';
    try {
      const { data } = await readGithub.rest.git.getRef({
        owner, repo, ref: 'heads/' + checkpointRef,
      });
      refSha = String(
        data && data.object && data.object.sha || ''
      ).toLowerCase();
    } catch (err) {
      const status = Number(err && err.status || 0);
      if (status === 404) return { kind: 'missing', status };
      return { kind: 'unavailable', status };
    }
    if (!/^[0-9a-f]{40}$/.test(refSha)) {
      return { kind: 'invalid', reason: 'ref-sha-malformed' };
    }
    let content;
    try {
      const response = await readGithub.rest.repos.getContent({
        owner,
        repo,
        path: '.continuum-429-recovery.json',
        ref: refSha,
      });
      content = response.data;
    } catch (err) {
      const status = Number(err && err.status || 0);
      if (status === 404 || status === 422) {
        return { kind: 'invalid', reason: 'metadata-missing', status };
      }
      return { kind: 'unavailable', status };
    }
    if (!content || Array.isArray(content) || content.type !== 'file') {
      return { kind: 'invalid', reason: 'metadata-not-file' };
    }
    let identity;
    try {
      identity = JSON.parse(
        Buffer.from(content.content || '', 'base64').toString('utf8')
      );
    } catch (err) {
      return { kind: 'invalid', reason: 'metadata-not-json' };
    }
    if (
      !identity ||
      typeof identity !== 'object' ||
      Array.isArray(identity)
    ) {
      return { kind: 'invalid', reason: 'metadata-not-object' };
    }
    if (Number(identity.schema_version) !== 1) {
      return { kind: 'invalid', reason: 'schema-version' };
    }
    if (String(identity.operation_id) !== String(operation)) {
      return { kind: 'invalid', reason: 'operation-mismatch' };
    }
    if (Number(identity.generation) !== Number(generation)) {
      return { kind: 'invalid', reason: 'generation-mismatch' };
    }
    if (String(identity.checkpoint_ref) !== String(checkpointRef)) {
      return { kind: 'invalid', reason: 'checkpoint-ref-mismatch' };
    }
    if (String(identity.mode) !== 'issue') {
      return { kind: 'invalid', reason: 'mode-mismatch' };
    }
    if (String(identity.issue_number || '') !== String(issueNumber)) {
      return { kind: 'invalid', reason: 'issue-mismatch' };
    }
    if (!/^[0-9a-f]{40}$/i.test(String(identity.head_sha || ''))) {
      return { kind: 'invalid', reason: 'head-sha-malformed' };
    }
    if (!String(identity.stage || '').trim()) {
      return { kind: 'invalid', reason: 'stage-missing' };
    }
    return { kind: 'valid', refSha, identity };
  }
  
  async function evaluateScheduler429Recovery({
    recovery, issueNumber,
  }) {
    const now = Date.now();
    // Fail-closed marker states are bounded by the marker comment's
    // age. Without this, an unreadable or malformed marker could keep
    // a reservation forever; the grace is separate from the removed
    // 48h catch-up horizon and only releases states nothing can act on.
    const INVALID_MARKER_GRACE_MS = 24 * 60 * 60 * 1000;
    const markerAtMs = Number(recovery && recovery.commentAt) || 0;
    const invalidStateGraceExpired =
      markerAtMs > 0 && now - markerAtMs > INVALID_MARKER_GRACE_MS;
    const base = {
      operation: recovery && recovery.operation || '',
      generation: recovery && recovery.generation || 0,
      checkpointRef: recovery && recovery.checkpointRef || '',
      runId: recovery && recovery.runId || '',
      state: recovery && recovery.state || '',
      keepReservation: false,
      countWip: false,
      recoveryPending: false,
      reason: 'no-recovery-state',
    };
    if (!recovery) return base;
    const identity = parseSchedulerCheckpointIdentity(
      recovery.checkpointRef
    );
    if (
      !identity ||
      identity.operation !== recovery.operation ||
      identity.generation !== Number(recovery.generation) ||
      !String(recovery.operation).split('-').includes(
        'issue' + issueNumber
      )
    ) {
      if (invalidStateGraceExpired) {
        return { ...base, reason: 'malformed-marker-expired' };
      }
      return {
        ...base,
        keepReservation: true,
        recoveryPending: true,
        reason: 'malformed-marker',
      };
    }
    const due = Math.max(0, Number(recovery.nextDue) || 0) * 1000;
    const state = String(recovery.state || '');
    if (state === 'metadata-failed') {
      if (due > now) {
        return {
          ...base,
          keepReservation: true,
          recoveryPending: true,
          reason: 'metadata-failed-lease',
        };
      }
      return { ...base, reason: 'metadata-failed-expired' };
    }
    if (
      due > now &&
      [
        'claimed', 'accepted', 'backoff', 'metadata-pending',
        'dispatch-failed', 'observed-unverified',
        'reconcile-pending',
      ].includes(state)
    ) {
      return {
        ...base,
        keepReservation: true,
        recoveryPending: true,
        reason: 'watchdog-lease',
      };
    }
    const run = await fetchSchedulerRecoveryRun(recovery.runId);
    if (run.kind === 'unavailable') {
      if (invalidStateGraceExpired) {
        return { ...base, reason: 'run-unavailable-expired' };
      }
      return {
        ...base,
        keepReservation: true,
        recoveryPending: true,
        reason: 'run-unavailable',
      };
    }
    if (
      run.kind === 'found' &&
      run.run &&
      run.run.status !== 'completed'
    ) {
      return {
        ...base,
        keepReservation: true,
        countWip: true,
        reason: 'run-active',
      };
    }
    const checkpoint = await readSchedulerCheckpointEvidence({
      operation: recovery.operation,
      generation: recovery.generation,
      checkpointRef: recovery.checkpointRef,
      issueNumber,
    });
    if (checkpoint.kind === 'unavailable') {
      if (invalidStateGraceExpired) {
        return { ...base, reason: 'checkpoint-unavailable-expired' };
      }
      return {
        ...base,
        keepReservation: true,
        recoveryPending: true,
        reason: 'checkpoint-unavailable',
      };
    }
    if (checkpoint.kind === 'valid') {
      if (
        run.kind === 'found' &&
        run.run &&
        run.run.status === 'completed' &&
        run.run.conclusion === 'success'
      ) {
        return { ...base, reason: 'run-completed-success' };
      }
      const markerSha = String(
        recovery.checkpointSha || ''
      ).toLowerCase();
      if (!/^[0-9a-f]{40}$/.test(markerSha)) {
        if (invalidStateGraceExpired) {
          return {
            ...base,
            reason: 'checkpoint-sha-invalid-expired',
          };
        }
        return {
          ...base,
          keepReservation: true,
          recoveryPending: true,
          reason: 'checkpoint-sha-invalid',
        };
      }
      const advanced = markerSha !== checkpoint.refSha;
      if (advanced) {
        return {
          ...base,
          keepReservation: true,
          recoveryPending: true,
          reason: 'checkpoint-awaiting-recovery',
          checkpointSha: checkpoint.refSha,
        };
      }
      return {
        ...base,
        reason: 'checkpoint-unchanged',
        checkpointSha: checkpoint.refSha,
      };
    }
    return { ...base, reason: 'checkpoint-' + checkpoint.kind };
  }
  
  // Owner-armed resume is evaluated only over the scheduler's
  // existing bounded open-issue list and only for paused+ready
  // targets. Comments are fetched lazily for those targets; every
  // read/write failure leaves the pause intact. The transition
  // removes exactly the armed target's pause label and lets the
  // normal recovery path (checkpoint/429 watchdog + dispatch) do
  // the work. It never edits task snapshots or starts a new
  // generation.
  const ownerResumeTargets = issues.filter(issue => {
    const labels = labelsByIssue.get(issue.number) || new Set();
    return (
      Boolean(readyLabel) &&
      labels.has(readyLabel) &&
      labels.has(pausedLabel) &&
      !labels.has(qualifyingLabel) &&
      !labels.has(blockedLabel)
    );
  });
  for (const issue of ownerResumeTargets) {
    let ownerResumeComments;
    try {
      ownerResumeComments = await ownerResumeIssueComments(issue.number);
    } catch (err) {
      core.warning(
        'Owner-armed resume comment read failed for #' + issue.number +
          '; failing closed: ' + err.message
      );
      continue;
    }
    const ownerResumeScanResult = ownerResumeScan(
      issue.number, ownerResumeComments
    );
    if (!ownerResumeScanResult.instruction) {
      if (ownerResumeScanResult.reason !== 'not owner-armed') {
        core.notice(
          'Owner-armed resume skipped for #' + issue.number + ': ' +
            ownerResumeScanResult.reason + '.'
        );
      }
      continue;
    }
    let ownerResumeDecisionResult;
    try {
      ownerResumeDecisionResult = await ownerResumeDecision(
        issue,
        ownerResumeScanResult.instruction,
        ownerResumeComments
      );
    } catch (err) {
      core.warning(
        'Owner-armed resume evaluation failed for #' + issue.number +
          '; failing closed: ' + err.message
      );
      continue;
    }
    if (ownerResumeDecisionResult.state !== 'RESUME') {
      core.notice(
        'Owner-armed resume for #' + issue.number + ' is ' +
          ownerResumeDecisionResult.state + ': ' +
          ownerResumeDecisionResult.reason + '.'
      );
      continue;
    }
    await removeLabel(issue.number, pausedLabel);
    core.notice(
      'Owner-armed resume for #' + issue.number +
        ' admitted after predecessor completion: ' +
        ownerResumeDecisionResult.reason +
        '; implementation PR #' + ownerResumeDecisionResult.pullRequest +
        ' at ' + ownerResumeDecisionResult.headSha +
        '. Checkpoint/snapshot state was not modified.'
    );
  }
  
  // Reconcile reservations for the gap between posting /oc and the PR
  // appearing. The dispatch comment is audit/timing metadata only; it
  // never blocks an issue forever.
  for (const issue of issues) {
    const issueNumber = issue.number;
    if (
      openPrIssues.has(issueNumber) ||
      activeRunIssues.has(issueNumber)
    ) continue;
  
    const labels = labelsByIssue.get(issueNumber);
    if (!labels.has(inProgressLabel)) continue;
  
    // When explicit admission is enabled, a non-admitted in-progress
    // issue belongs to another lane (qualification/control/manual).
    // Leave its lifecycle label intact but do not reconcile/count it
    // as automatic implementation WIP.
    if (readyLabel && !labels.has(readyLabel)) {
      continue;
    }
  
    // Qualification lifecycle is not implementation WIP. Once the
    // implementation run is gone, release any stale reservation so a
    // qualifying/qualification-blocked capability cannot consume a
    // coding slot or be re-dispatched as ordinary implementation.
    if (labels.has(qualifyingLabel) || labels.has(blockedLabel)) {
      await removeLabel(issueNumber, inProgressLabel);
      core.notice(
        'Released implementation reservation on issue #' + issueNumber +
        ': capability is in qualification lifecycle.'
      );
      continue;
    }
  
    // A native open dependency is authoritative even when an older
    // dispatch lease still exists. Once the actual OpenCode run is
    // gone, release the reservation immediately so blocked work does
    // not consume WIP capacity or get resurrected from old comments.
    // Snapshot-scoped read shared with candidate selection (A2).
    const reservationBlockers = await snapshotNativeBlockers(issueNumber);
    const openReservationBlockers = reservationBlockers.filter(
      blocker => blocker.state === 'open'
    );
    if (openReservationBlockers.length > 0) {
      await removeLabel(issueNumber, inProgressLabel);
      core.notice(
        'Released reservation on issue #' + issueNumber +
        ': blocked by ' +
        openReservationBlockers
          .map(blocker => '#' + blocker.number)
          .join(', ') + '.'
      );
      continue;
    }
  
    const comments = await readGithub.paginate(
      readGithub.rest.issues.listComments,
      {
        owner,
        repo,
        issue_number: issueNumber,
        per_page: 100,
      }
    );
    const commentClasses = classifyComments(
      comments.map(comment => comment.body || '')
    );
    const dispatches = comments
      .filter((comment, index) => {
        const body = comment.body || '';
        const ownerOpenCodeCommand =
          comment.user?.login === owner &&
          commentClasses[index] === 'run';
        return body.includes(dispatchMarker) || ownerOpenCodeCommand;
      })
      .sort(
        (a, b) =>
          new Date(a.created_at).getTime() -
          new Date(b.created_at).getTime()
      );
    // Only the scheduler's own dispatches consume the attempt budget;
    // a manual owner /oc must never burn one.
    const schedulerDispatches = comments.filter(comment =>
      (comment.body || '').includes(dispatchMarker)
    );
    // A verified owner marker scopes a fresh, bounded retry budget
    // for this exact target. Pre-marker infrastructure attempts are
    // history; attempts after admission remain bounded.
    const ownerResumeMarkerBaselineMs = ownerResumeBaselineNumber(
      comments, issueNumber
    );
    const ownerResumeMarkerPending =
      ownerResumeMarkerBaselineMs !== null &&
      labels.has(pausedLabel);
    const ownerResumeBaselineMs = ownerResumeMarkerPending
      ? null
      : ownerResumeMarkerBaselineMs;
    const countedSchedulerDispatches =
      ownerResumeBaselineMs === null
        ? schedulerDispatches
        : schedulerDispatches.filter(comment =>
            new Date(comment.created_at || 0).getTime() >
              ownerResumeBaselineMs
          );
    // Durable 429 recovery state is infrastructure, never a task
    // failure. While it is fresh, the scheduler leaves the
    // reservation with the watchdog catch-up instead of burning
    // dispatch attempts or applying automation:paused.
    const recoveryComments = comments.filter(comment => {
      const body = comment.body || '';
      return /<!--\s*continuum-429-(?:recovery|resumption|backoff)\b/.test(body);
    });
    const newestRecovery = recoveryComments.length
      ? latestSchedulerRecoveryState(comments)
      : null;
    // The durable marker is only an ownership claim while the exact
    // run and the metadata-verified checkpoint agree. `observed`
    // with next_due=0 is not a 48h lease: the exact run GET decides.
    const recoveryEvaluation = await evaluateScheduler429Recovery({
      recovery: newestRecovery,
      issueNumber,
    });
    const hasFresh429Recovery = recoveryEvaluation.keepReservation;
    const latestDispatch = dispatches.at(-1);
    const latestPr = latestPrByIssue.get(issueNumber);
  
    // If a PR from the current/latest dispatch already completed but
    // its event was missed, reconcile it here.
    if (latestDispatch && latestPr) {
      const dispatchAt = new Date(latestDispatch.created_at).getTime();
      const prAt = new Date(latestPr.created_at).getTime();
      if (prAt >= dispatchAt && latestPr.state === 'closed') {
        if (latestPr.merged_at) {
          let mergeSha = null;
          try {
            const fullPr = (await readGithub.rest.pulls.get({
              owner,
              repo,
              pull_number: latestPr.number,
            })).data;
            mergeSha = normalizeSha(fullPr.merge_commit_sha);
          } catch (err) {
            if (err.status !== 404) throw err;
          }
          const entered = await enterQualifyingState(
            issueNumber,
            mergeSha,
            'reconciled PR #' + latestPr.number
          );
          if (entered === 'blocked-waiting') {
            core.notice(
              'Reconciled merged PR #' + latestPr.number +
              ' -> capability #' + issueNumber +
              ' stays blocked; qualification waits for blockers.'
            );
          } else if (entered === 'missing' || entered.length === 0) {
            await removeLabel(issueNumber, inProgressLabel);
            await removeLabel(issueNumber, pausedLabel);
            await github.rest.issues.update({
              owner,
              repo,
              issue_number: issueNumber,
              state: 'closed',
              state_reason: 'completed',
            });
            updateOpenSnapshot({ number: issueNumber, state: 'closed' });
            invalidateNativeBlockers();
            core.notice(
              'Reconciled merged PR #' + latestPr.number +
              ' -> completed issue #' + issueNumber + '.'
            );
          } else {
            core.notice(
              'Reconciled merged PR #' + latestPr.number +
              ' -> capability #' + issueNumber +
              ' enters qualification instead of completing.'
            );
          }
        } else {
          await pauseIssue(
            issueNumber,
            'PR #' + latestPr.number + ' closed without merge.'
          );
        }
        continue;
      }
    }
  
    const retainVerified429Recovery = () => {
      if (recoveryEvaluation.countWip) {
        activeIssueNumbers.add(issueNumber);
      } else {
        // Keep the reservation label so the bounded watchdog
        // discovery can find the durable marker, but release the
        // coding WIP slot: this is infrastructure recovery, not a
        // coding run. Candidate selection below skips this issue.
        recoveryPendingIssueNumbers.add(issueNumber);
      }
      core.notice(
        'Issue #' + issueNumber +
        ' has verified durable 429 infrastructure recovery state (' +
        recoveryEvaluation.reason + '; op=' +
        (recoveryEvaluation.operation || '-') + ' gen=' +
        (recoveryEvaluation.generation || 0) + ' checkpoint=' +
        (recoveryEvaluation.checkpointRef || '-') + ' run=' +
        (newestRecovery && newestRecovery.runId || '-') +
        '); ' +
        (recoveryEvaluation.countWip
          ? 'counting its active run as WIP.'
          : 'retaining the reservation for the watchdog without ' +
            'consuming a coding dispatch attempt or WIP slot.')
      );
    };
  
    if (!latestDispatch) {
      // A durable recovery marker is itself evidence of ownership:
      // the watchdog may have dispatched from a manual /oc or a
      // workflow_run without a scheduler dispatch record. Releasing
      // the label here would make that checkpoint undiscoverable.
      if (hasFresh429Recovery) {
        retainVerified429Recovery();
        continue;
      }
      // A stale/manual reservation with no evidence of a dispatch is
      // safe to release immediately.
      await removeLabel(issueNumber, inProgressLabel);
      core.warning(
        'Released stale reservation on issue #' + issueNumber +
        ': no scheduler dispatch record exists.'
      );
      continue;
    }
  
    const ageMs =
      Date.now() - new Date(latestDispatch.created_at).getTime();
  
    if (ageMs < leaseMs) {
      activeIssueNumbers.add(issueNumber);
      continue;
    }
  
    if (hasFresh429Recovery) {
      retainVerified429Recovery();
      continue;
    }
  
    await removeLabel(issueNumber, inProgressLabel);
  
    if (
      countedSchedulerDispatches.length >= maxDispatchAttempts &&
      pauseOnFailure &&
      !hasFresh429Recovery
    ) {
      await addLabel(issueNumber, pausedLabel);
      core.warning(
        'Issue #' + issueNumber + ' produced no OpenCode PR after ' +
        countedSchedulerDispatches.length +
        ' scheduler dispatch attempt(s); automation paused.'
      );
    } else if (ownerResumeMarkerPending) {
      // The predecessor is not yet provably complete. Keep the pause
      // label and do not release an infrastructure-stalled generation.
      core.notice(
        'Owner-armed resume for issue #' + issueNumber +
        ' is waiting for predecessor completion.'
      );
    } else {
      await removeLabel(issueNumber, pausedLabel);
      core.warning(
        'Lease expired for issue #' + issueNumber +
        '; it is eligible for an automatic retry' +
        (countedSchedulerDispatches.length >= maxDispatchAttempts
          ? ' because pause_on_failure=false.'
          : '.')
      );
    }
  }
  
  const totalActiveWip = activeIssueNumbers.size + childActiveWip;
  const freeSlots = Math.max(0, wipLimit - totalActiveWip);
  core.notice(
    'WIP: ' + totalActiveWip + '/' + wipLimit +
    ' (local ' + activeIssueNumbers.size +
    ', delegated ' + childActiveWip + ')' +
    '; free slots: ' + freeSlots +
    '; active local issues: ' +
    ([...activeIssueNumbers]
      .sort((a, b) => a - b)
      .map(n => '#' + n)
      .join(', ') || 'none') + '.'
  );
  
  // Qualification/validation trackers are never generic
  // implementation work (kodmial/continuum#278): they need explicit
  // qualification dispatch, independent of automation:ready. The set
  // is derived from the open inventory without extra API reads.
  const qualificationTrackers = collectQualificationTrackers(issues);
  
  if (freeSlots === 0) {
    // Cron is the missed-event backstop (kodmial/continuum#280): a
    // pass that leaves eligible backlog unexplained while reporting
    // success can stall autonomous development silently for days.
    // Name every eligible-looking waiter (label-level admission
    // only, so no extra API reads) with its priority and the WIP
    // holders blocking it. When such backlog waits the run must
    // fail instead of reporting success with zero progress; a full
    // WIP with no waiters is routine busy state and stays green.
    const holderDetails = [...activeIssueNumbers]
      .sort((a, b) => a - b)
      .map(number => {
        const holderLabels = labelsByIssue.get(number) || new Set();
        const holderPriorities = [...holderLabels].filter(name =>
          priorityRank.has(name)
        );
        return '#' + number + ' (' +
          (holderPriorities[0] || 'no priority') + ')';
      })
      .join(', ') || 'none';
    const wipWaiters = [];
    for (const issue of issues) {
      if (activeIssueNumbers.has(issue.number)) continue;
      if (openPrIssues.has(issue.number)) continue;
      if (recoveryPendingIssueNumbers.has(issue.number)) continue;
      const admission = shouldAdmitToImplementation(
        issue.number, qualificationTrackers
      );
      if (!admission.admit) continue;
      const waiterLabels = labelsByIssue.get(issue.number);
      if (pauseOnFailure && waiterLabels.has(pausedLabel)) continue;
      if (waiterLabels.has(qualifyingLabel) || waiterLabels.has(blockedLabel)) continue;
      const waiterPriorities = [...waiterLabels]
        .filter(name => priorityRank.has(name))
        .sort((a, b) => priorityRank.get(a) - priorityRank.get(b));
      const waiterPriority = waiterPriorities[0] || null;
      if (requirePriorityLabel && waiterPriority === null) continue;
      if (readyLabel && !waiterLabels.has(readyLabel)) continue;
      wipWaiters.push(issue.number);
      core.warning(
        'Skipping #' + issue.number +
        ': waiting for a free WIP slot (' +
        (waiterPriority || 'no priority') +
        '); active WIP holders: ' + holderDetails + '.'
      );
    }
    if (wipWaiters.length > 0) {
      core.notice(
        'WIP is saturated; ' + wipWaiters.length +
        ' eligible issue(s) remain queued and will be retried by the next lifecycle or scheduled wake.'
      );
    } else {
      core.info('No free WIP slots; nothing will be dispatched.');
    }
    return;
  }
  
  const candidates = [];
  for (const issue of issues) {
    if (activeIssueNumbers.has(issue.number)) {
      core.info(
        'Skipping #' + issue.number +
        ': WIP slot held by an active reservation or run.'
      );
      continue;
    }
    // An existing implementation PR must never be duplicated even
    // when it is configured not to consume a WIP slot.
    if (openPrIssues.has(issue.number)) {
      core.info(
        'Skipping #' + issue.number +
        ': open implementation PR must not be duplicated.'
      );
      continue;
    }
    // Verified 429 checkpoints are recovered by the watchdog from
    // their exact SHA. They must never be restarted from main just
    // because they no longer consume a coding WIP slot.
    if (recoveryPendingIssueNumbers.has(issue.number)) {
      core.info(
        'Skipping #' + issue.number +
        ': verified 429 checkpoint is awaiting watchdog recovery.'
      );
      continue;
    }
  
    const admission = shouldAdmitToImplementation(
      issue.number, qualificationTrackers
    );
    if (!admission.admit) {
      core.info(
        'Skipping #' + issue.number +
        ': qualification tracker needs explicit dispatch, not generic implementation.'
      );
      continue;
    }
  
    const labels = labelsByIssue.get(issue.number);
    if (pauseOnFailure && labels.has(pausedLabel)) {
      core.info('Skipping #' + issue.number + ': automation is paused.');
      continue;
    }
    if (labels.has(qualifyingLabel) || labels.has(blockedLabel)) {
      core.info(
        'Skipping #' + issue.number +
        ': capability is in qualification lifecycle.'
      );
      continue;
    }
  
    const priorities = [...labels]
      .filter(name => priorityRank.has(name))
      .sort(
        (a, b) => priorityRank.get(a) - priorityRank.get(b)
      );
  
    // Multiple priority labels are not a reason to deadlock an issue.
    // Highest priority wins deterministically; the anomaly is logged.
    const priority = priorities[0] || null;
    if (requirePriorityLabel && priority === null) {
      core.info(
        'Skipping #' + issue.number + ': no priority label and ' +
          'require_priority_label is enabled.'
      );
      continue;
    }
    const rank = priority === null ? priorityOrder.length : priorityRank.get(priority);
    if (priorities.length > 1) {
      core.warning(
        'Issue #' + issue.number + ' has multiple priority labels (' +
        priorities.join(', ') + '); using ' + priority + '.'
      );
    }
    // Optional admission gate: only an explicitly admitted issue may
    // enter automatic implementation. Empty keeps current behavior;
    // priority remains a ranking signal only.
    if (readyLabel && !labels.has(readyLabel)) {
      core.info(
        'Skipping #' + issue.number + ': missing required ready label `' +
          readyLabel + '`.'
      );
      continue;
    }
  
    // Terminal generation stops never enter the queue: no WIP,
    // no /oc, no redispatch. Successors stay schedulable.
    const localStop = await terminalGenerationStop(
      owner, repo, issue.number, readGithub
    );
    if (localStop.stopped) {
      core.info(
        'Skipping #' + issue.number + ': ' +
        terminalStopSkipReason(issue.number, localStop)
      );
      continue;
    }
  
    candidates.push({ source: 'local', issue, priority, rank });
  }
  
  for (const child of childCandidates) {
    const taskNumber = Number.parseInt(String(child.task_number), 10);
    const rank = Number.parseInt(String(child.rank), 10);
    if (
      !child.child_id ||
      !child.repository ||
      !Number.isInteger(taskNumber) ||
      taskNumber < 1 ||
      !Number.isInteger(rank) ||
      rank < 0
    ) {
      throw new Error(
        'Invalid delegated task candidate.'
      );
    }
    candidates.push({
      source: 'child',
      issue: {
        number: taskNumber,
        title: child.title || '',
        body: '',
      },
      priority: child.priority || null,
      rank,
      child: {
        child_id: String(child.child_id),
        repository: String(child.repository),
        task_number: taskNumber,
      },
    });
  }
  
  candidates.sort(
    (a, b) =>
      a.rank - b.rank ||
      a.issue.number - b.issue.number ||
      a.source.localeCompare(b.source) ||
      String(a.child?.repository || '').localeCompare(
        String(b.child?.repository || '')
      )
  );
  
  const selected = [];
  for (const candidate of candidates) {
    if (selected.length >= freeSlots) {
      core.info(
        'Skipping #' + candidate.issue.number +
        ': waiting for a free WIP slot (' +
        (candidate.priority || 'no priority') +
        '); selected ahead: ' +
        (selected
          .map(item => '#' + item.issue.number)
          .join(', ') || 'none') + '.'
      );
      continue;
    }
  
    const issue = candidate.issue;
    if (candidate.source === 'child') {
      selected.push(candidate);
      continue;
    }
  
    const declaredOpenBlockers = await openDeclaredBlockers(issue);
    const candidateRefs = qualificationRefs(issue.body || '', issue.number);
    const candidateSplit = effectiveQualificationBlockers(
      candidateRefs,
      declaredOpenBlockers.map(blocker => blocker.number)
    );
    if (candidateSplit.ignored.length > 0) {
      core.notice(
        'Issue #' + issue.number +
        ': ignoring qualification back-edge blocker(s) ' +
        candidateSplit.ignored.map(number => '#' + number).join(', ') +
        ' for implementation readiness.'
      );
    }
    if (candidateSplit.effective.length > 0) {
      core.info(
        'Skipping #' + issue.number + ': declared blocked by ' +
        candidateSplit.effective.map(number => '#' + number).join(', ') + '.'
      );
      continue;
    }
  
    // Snapshot-scoped read shared with reservation reconciliation
    // (A2); the fresh pre-dispatch query below always re-reads.
    const blockers = await snapshotNativeBlockers(issue.number);
    const openBlockers = blockers.filter(
      blocker => blocker.state === 'open'
    );
    if (openBlockers.length > 0) {
      core.info(
        'Skipping #' + issue.number + ': blocked by ' +
        openBlockers.map(blocker => '#' + blocker.number).join(', ') + '.'
      );
      continue;
    }
  
    selected.push(candidate);
  }
  
  if (selected.length === 0) {
    core.notice('No eligible unblocked issues are ready.');
    return;
  }
  
  core.notice(
    'Selected: ' +
    selected
      .map(item =>
        (item.source === 'child'
          ? 'SubTask #' + item.issue.number
          : '#' + item.issue.number) +
        ' (' + (item.priority || 'no priority') + ')'
      )
      .join(', ') + '.'
  );
  
  // Snapshot admission ends here: every guard inside the dispatch
  // loop below re-reads fresh state (issue/PR open, labels,
  // declared and native blockers, owner-command race window).
  useOpenSnapshot = false;
  
  for (const candidate of selected) {
    const { issue, priority } = candidate;
  
    if (candidate.source === 'child') {
      const child = candidate.child;
      const parts = child.repository.split('/');
      if (parts.length !== 2 || !parts[0] || !parts[1]) {
        throw new Error(
          'Invalid delegated repository binding.'
        );
      }
      const [childOwner, childRepo] = parts;
      const freshResponse = await github.rest.issues.get({
        owner: childOwner,
        repo: childRepo,
        issue_number: child.task_number,
      });
      const freshChildIssue = freshResponse.data;
      if (
        freshChildIssue.state !== 'open' ||
        freshChildIssue.pull_request
      ) {
        core.info(
          'Skipping SubTask #' + child.task_number +
          ': state changed before dispatch.'
        );
        continue;
      }
      // Optional admission gate, re-verified just in time: a label
      // removed after selection must prevent dispatch.
      if (readyLabel) {
        const freshChildLabels = new Set(
          (freshChildIssue.labels || []).map(label =>
            typeof label === 'string' ? label : label.name
          )
        );
        if (!freshChildLabels.has(readyLabel)) {
          core.info(
            'Skipping SubTask #' + child.task_number +
            ': missing required ready label `' + readyLabel + '`.'
          );
          continue;
        }
      }
  
      const childDeclaredBlockers = await openDeclaredBlockers(
        freshChildIssue,
        childOwner,
        childRepo
      );
      if (childDeclaredBlockers.length > 0) {
        core.info(
          'Skipping SubTask #' + child.task_number +
          ': declared blocker appeared before dispatch.'
        );
        continue;
      }
  
      const childNativeBlockers = await github.paginate(
        'GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by',
        {
          owner: childOwner,
          repo: childRepo,
          issue_number: child.task_number,
          per_page: 100,
          headers: {
            'X-GitHub-Api-Version': '2022-11-28',
          },
        }
      );
      if (
        childNativeBlockers.some(blocker => blocker.state === 'open')
      ) {
        core.info(
          'Skipping SubTask #' + child.task_number +
          ': native blocker appeared before dispatch.'
        );
        continue;
      }
  
      const childStop = await terminalGenerationStop(
        childOwner, childRepo, child.task_number, github
      );
      if (childStop.stopped) {
        core.info(
          'Skipping SubTask #' + child.task_number + ': ' +
          terminalStopSkipReason(child.task_number, childStop)
        );
        continue;
      }
  
      // Smallest authoritative recheck for the selection-to-
      // dispatch race: a run that appeared after the pass snapshot
      // (or a same-pass dispatch) must suppress the new job. An
      // uncertain read also suppresses dispatch; the next wake
      // retries when the inventory is trustworthy again.
      const taskKey =
        child.child_id + '#' + child.task_number;
      if (
        dispatchedTaskKeys.has(taskKey) ||
        freshDelegatedTaskActive(
          child.child_id, child.task_number
        )
      ) {
        core.info(
          'Skipping SubTask #' + child.task_number +
          ': an identical active or just-dispatched run already ' +
          'owns this task.'
        );
        continue;
      }
  
      await dispatchWorkflow(childWorkerWorkflow, {
        child_id: child.child_id,
        task_number: String(child.task_number),
      });
      dispatchedTaskKeys.add(taskKey);
      core.notice(
        'Dispatched SubTask #' + child.task_number + ' (' +
        (priority || 'no priority') + ').'
      );
      continue;
    }
    // Re-check mutable state immediately before dispatch. Another
    // owner /oc, dependency update, pause, or reservation may have
    // landed while this scheduler run was already evaluating the
    // backlog. Without this JIT guard an older scheduler run can
    // enqueue a duplicate OpenCode run after a manual /oc.
    const freshIssueResponse = await readGithub.rest.issues.get({
      owner,
      repo,
      issue_number: issue.number,
    });
    const freshIssue = freshIssueResponse.data;
    const freshLabels = new Set(
      (freshIssue.labels || []).map(label =>
        typeof label === 'string' ? label : label.name
      )
    );
  
    if (
      freshIssue.state !== 'open' ||
      (pauseOnFailure && freshLabels.has(pausedLabel)) ||
      freshLabels.has(inProgressLabel)
    ) {
      core.info(
        'Skipping #' + issue.number +
        ': state changed before dispatch.'
      );
      continue;
    }
    // Optional admission gate, re-verified just in time: a label
    // removed after selection must prevent dispatch.
    if (readyLabel && !freshLabels.has(readyLabel)) {
      core.info(
        'Skipping #' + issue.number +
        ': missing required ready label `' + readyLabel + '`.'
      );
      continue;
    }
  
    // Terminal generation stops hold through dispatch: a stop
    // published after selection still prevents this run.
    const freshStop = await terminalGenerationStop(
      owner, repo, issue.number, readGithub
    );
    if (freshStop.stopped) {
      core.info(
        'Skipping #' + issue.number + ': ' +
        terminalStopSkipReason(issue.number, freshStop)
      );
      continue;
    }
  
    // A qualification tracker re-verified here is still never
    // generic implementation work, even if it appeared after
    // selection.
    const freshAdmission = shouldAdmitToImplementation(
      freshIssue.number, qualificationTrackers
    );
    if (!freshAdmission.admit) {
      core.info(
        'Skipping #' + issue.number +
        ': qualification tracker needs explicit dispatch, not generic implementation.'
      );
      continue;
    }
    const freshDeclaredBlockers = await openDeclaredBlockers(freshIssue);
    const freshSplit = effectiveQualificationBlockers(
      qualificationRefs(freshIssue.body || '', freshIssue.number),
      freshDeclaredBlockers.map(blocker => blocker.number)
    );
    if (freshSplit.effective.length > 0) {
      core.info(
        'Skipping #' + issue.number + ': declared blocker appeared before dispatch: ' +
        freshSplit.effective.map(number => '#' + number).join(', ') + '.'
      );
      continue;
    }
  
    const freshBlockers = await readGithub.paginate(
      'GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by',
      {
        owner,
        repo,
        issue_number: issue.number,
        per_page: 100,
        headers: {
          'X-GitHub-Api-Version': '2022-11-28',
        },
      }
    );
    const freshOpenBlockers = freshBlockers.filter(
      blocker => blocker.state === 'open'
    );
    if (freshOpenBlockers.length > 0) {
      core.info(
        'Skipping #' + issue.number +
        ': blocker appeared before dispatch: ' +
        freshOpenBlockers
          .map(blocker => '#' + blocker.number)
          .join(', ') + '.'
      );
      continue;
    }
  
    // Bounded grace-window guard (A3): one small recent query,
    // always fresh, preserves owner identity and both spellings.
    const latestOpenCodeCommand = await recentOwnerCommandWithinGrace(issue.number);
  
    if (latestOpenCodeCommand) {
      const commandAgeMs =
        Date.now() -
        new Date(latestOpenCodeCommand.created_at).getTime();
      if (commandAgeMs < commandGraceMs) {
        core.info(
          'Skipping #' + issue.number +
          ': an owner OpenCode command was posted within the ' +
          'short dispatch race window.'
        );
        continue;
      }
    }
  
    // A label acts as a short lease/reservation while OpenCode starts.
    // It is rolled back immediately if posting /oc fails, expires
    // automatically if no PR appears, and is reconciled from PR state.
    await addLabel(issue.number, inProgressLabel);
  
    try {
      if (resetMarkers) {
        // Opt-in: drop previously recorded dispatch markers so the
        // issue starts from a fresh attempt budget. Consumers that
        // rely on markers as an audit trail leave this off.
        const previous = await readGithub.paginate(
          'GET /repos/{owner}/{repo}/issues/{issue_number}/comments',
          {
            owner,
            repo,
            issue_number: issue.number,
            per_page: 100,
          }
        );
        for (const comment of previous) {
          if ((comment.body || '').includes(dispatchMarker)) {
            await github.rest.issues.deleteComment({
              owner,
              repo,
              comment_id: comment.id,
            });
          }
        }
      }
  
      const markerComment = [
        dispatchMarker,
        '⚡ **Continuum · issue-scheduler**',
        '<!-- continuum-origin role=continuum component=issue-scheduler -->',
        'Automatically dispatched by the issue scheduler (' +
          (priority || 'no explicit priority') + ').',
      ].join('\n');
  
      const route = executionRoutes.find(
        candidate => issue.labels.some(l => l.name === candidate.label)
      );
  
      if (route) {
        // The marker still lands on the issue so a consumer can see
        // the attempt in the audit trail, but the workflow is
        // dispatched directly: an execution workflow does not read
        // owner comments.
        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: issue.number,
          body: markerComment,
        });
        await dispatchWorkflow(route.workflow, {
          ...route.inputs,
          issue_number: String(issue.number),
        });
      } else if (opencodeDispatch === 'workflow') {
        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: issue.number,
          body: markerComment,
        });
        // Inlined rather than routed through dispatchWorkflow so
        // the dispatched mode stays visible at the call site.
        await github.request(
          'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches',
          {
            owner,
            repo,
            workflow_id: opencodeWorkflowPath.replace(
              /^\.github\/workflows\//,
              ''
            ),
            ref: dispatchRef,
            inputs: { mode: 'issue', issue_number: String(issue.number) },
          }
        );
      } else {
        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: issue.number,
          body: ['/oc', '', markerComment].join('\n'),
        });
      }
    } catch (err) {
      await removeLabel(issue.number, inProgressLabel);
      throw err;
    }
  
    core.notice(
      'Dispatched issue #' + issue.number + ' (' +
      (priority || 'no priority') + ').'
    );
  }
};
