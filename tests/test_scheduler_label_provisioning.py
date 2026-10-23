"""Counted label-provisioning requests for kodmial/continuum#388.

The scheduler used to POST every label spec on every reconciliation pass.
Existing labels answered with HTTP 422, but each redundant write still spent a
shared ``TAP_PAT`` core request. These tests execute the shipped
``buildSchedulerLabelSpecs`` and ``provisionSchedulerLabels`` functions extracted
from ``continuum-issue-scheduler.yml`` against a deterministic fake GitHub
client, so the measured endpoints and credentials belong to the real bootstrap.

Covered Definition of Done items:

* ten no-change passes issue zero ``POST /labels``;
* first initialization creates exactly the missing specs and the next pass
  creates none; a concurrent 422 stays idempotent;
* more than 100 labels spanning pages never hide a required label and unrelated
  labels are never deleted;
* 403 (primary limit), 429, 5xx, timeout, permission denial and incomplete
  inventory fail closed without a create fan-out, and a later healthy wake
  succeeds;
* route labels, the optional ready label, canonical priority labels and
  same-repository Parent-local scope are preserved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"

CANONICAL_LABELS = [
    "priority:p0",
    "priority:p1",
    "priority:p2",
    "automation:in-progress",
    "automation:paused",
    "automation:qualifying",
    "automation:blocked",
]


def scheduler_body() -> str:
    return SCHEDULER.read_text(encoding="utf-8")


def extract_function(body: str, name: str) -> str:
    needle = "function " + name + "("
    start = body.index(needle)
    if body[max(0, start - len("async ")):start] == "async ":
        start -= len("async ")
    signature_end = body.index(")", body.index("(", start))
    brace = body.index("{", signature_end)
    depth = 0
    for position in range(brace, len(body)):
        if body[position] == "{":
            depth += 1
        elif body[position] == "}":
            depth -= 1
            if depth == 0:
                return body[start:position + 1]
    raise AssertionError("could not extract JS function: " + name)


HARNESS = r"""
const assert = require('assert');

const CANONICAL_LABELS = [
  'priority:p0',
  'priority:p1',
  'priority:p2',
  'automation:in-progress',
  'automation:paused',
  'automation:qualifying',
  'automation:blocked',
];

class FakeGithub {
  constructor(labels, options) {
    this.labels = labels;
    this.options = options || {};
    this.listCalls = 0;
    this.createCalls = 0;
    this.deleteCalls = 0;
    this.requests = [];
    this.rest = {
      issues: {
        listLabelsForRepo: async params => this.list(params),
        createLabel: async params => this.create(params),
        deleteLabel: async params => {
          this.deleteCalls += 1;
          this.requests.push({
            method: 'DELETE',
            endpoint: '/labels',
            credential: 'TAP_PAT',
            params,
          });
          throw new Error('the scheduler must never delete labels');
        },
      },
    };
  }

  async list(params) {
    this.listCalls += 1;
    this.requests.push({
      method: 'GET',
      endpoint: '/labels',
      credential: 'GITHUB_TOKEN',
      page: params.page || 1,
      params,
    });
    if (this.options.listError) throw this.options.listError;
    const perPage = params.per_page || 30;
    const page = params.page || 1;
    const start = (page - 1) * perPage;
    const pageLabels = this.labels.slice(start, start + perPage);
    return { data: pageLabels.map(name => ({ name })) };
  }

  async create(params) {
    this.createCalls += 1;
    this.requests.push({
      method: 'POST',
      endpoint: '/labels',
      credential: 'TAP_PAT',
      name: params.name,
      color: params.color,
      description: params.description,
      params,
    });
    if (this.options.createError) throw this.options.createError;
    if (this.options.createHook) {
      const outcome = this.options.createHook(params, this.labels);
      if (outcome === 'created') {
        this.labels.push(params.name);
      }
      if (outcome instanceof Error) throw outcome;
      return { data: { name: params.name } };
    }
    if (this.labels.includes(params.name)) {
      const error = new Error('Validation Failed');
      error.status = 422;
      throw error;
    }
    this.labels.push(params.name);
    return { data: { name: params.name } };
  }

  async paginate(method, params) {
    const all = [];
    let page = 1;
    for (;;) {
      const response = await method({ ...params, page });
      if (!Array.isArray(response.data)) {
        return response.data;
      }
      all.push(...response.data);
      if (response.data.length < (params.per_page || 30)) break;
      page += 1;
      if (page > 1000) throw new Error('pagination did not terminate');
    }
    return all;
  }
}

function clients(labels, options) {
  const read = new FakeGithub(labels, options);
  const write = new FakeGithub(labels, options);
  return { read, write };
}

function config(overrides) {
  return {
    inProgressLabel: 'automation:in-progress',
    pausedLabel: 'automation:paused',
    qualifyingLabel: 'automation:qualifying',
    blockedLabel: 'automation:blocked',
    executionRouteLabels: new Set(['route:render']),
    readyLabel: 'automation:ready',
    ...(overrides || {}),
  };
}

async function provision(read, write, specs, repositoryOwner, repositoryName) {
  await provisionSchedulerLabels({
    readClient: read,
    writeClient: write,
    repositoryOwner: repositoryOwner || 'parent-owner',
    repositoryName: repositoryName || 'parent-repo',
    specs,
  });
}

function emit(result) {
  console.log('RESULT ' + JSON.stringify(result));
}

const scenarios = {
  async steady_state() {
    const labels = [...CANONICAL_LABELS, 'route:render', 'automation:ready'];
    const specs = buildSchedulerLabelSpecs(config());
    assert.strictEqual(specs.length, 9);
    const { read, write } = clients(labels);
    const legacyCreates = specs.length * 10;
    for (let pass = 0; pass < 10; pass += 1) {
      await provision(read, write, specs);
    }
    assert.strictEqual(write.createCalls, 0);
    assert.strictEqual(write.deleteCalls, 0);
    assert.strictEqual(read.listCalls, 10);
    emit({
      passes: 10,
      before: { 'POST /labels': legacyCreates, credential: 'TAP_PAT' },
      after: {
        'GET /labels': read.listCalls,
        credential: 'GITHUB_TOKEN',
        'POST /labels': write.createCalls,
        write_credential: 'TAP_PAT',
      },
      remaining_labels: labels.length,
    });
  },

  async first_initialization() {
    const labels = ['priority:p0'];
    const specs = buildSchedulerLabelSpecs(config());
    const missing = specs.map(spec => spec.name).filter(name => !labels.includes(name));
    const { read, write } = clients(labels);
    await provision(read, write, specs);
    assert.deepStrictEqual(
      write.requests.filter(r => r.method === 'POST').map(r => r.name),
      missing
    );
    assert.strictEqual(write.createCalls, missing.length);
    const captured = Object.fromEntries(
      write.requests
        .filter(r => r.method === 'POST')
        .map(r => [r.name, { color: r.color, description: r.description }])
    );
    for (const spec of specs) {
      if (missing.includes(spec.name)) {
        assert.deepStrictEqual(captured[spec.name], {
          color: spec.color,
          description: spec.description,
        });
      }
    }
    const second = clients(labels);
    await provision(second.read, second.write, specs);
    assert.strictEqual(second.write.createCalls, 0);
    assert.strictEqual(labels.length, specs.length);
    emit({
      first_pass: { 'GET /labels': read.listCalls, 'POST /labels': write.createCalls },
      second_pass: {
        'GET /labels': second.read.listCalls,
        'POST /labels': second.write.createCalls,
      },
      missing,
    });
  },

  async concurrent_422() {
    const labels = [...CANONICAL_LABELS];
    const specs = buildSchedulerLabelSpecs(config());
    const { read, write } = clients(labels, {
      createHook: (params, store) => {
        if (params.name === 'route:render') {
          store.push(params.name);
          const error = new Error('Validation Failed');
          error.status = 422;
          return error;
        }
        store.push(params.name);
        return 'created';
      },
    });
    await provision(read, write, specs);
    const routeCreates = write.requests.filter(
      r => r.method === 'POST' && r.name === 'route:render'
    );
    assert.strictEqual(routeCreates.length, 1);
    assert.strictEqual(write.createCalls, 2);
    const second = clients(labels);
    await provision(second.read, second.write, specs);
    assert.strictEqual(second.write.createCalls, 0);
    emit({
      race_creates: routeCreates.length,
      first_pass_creates: write.createCalls,
      second_pass_creates: second.write.createCalls,
    });
  },

  async pagination_and_unrelated_labels() {
    const unrelated = Array.from({ length: 141 }, (_, index) => 'unrelated:' + index);
    const labels = [...unrelated, ...CANONICAL_LABELS, 'route:render', 'automation:ready'];
    const specs = buildSchedulerLabelSpecs(config());
    assert.ok(labels.length >= 150);
    const { read, write } = clients(labels);
    await provision(read, write, specs);
    assert.strictEqual(write.createCalls, 0);
    assert.ok(read.listCalls >= 2, 'must read every page of a 101+ label inventory');
    const before = [...labels];

    labels.splice(labels.indexOf('route:render'), 1);
    const missing = clients(labels);
    await provision(missing.read, missing.write, specs);
    assert.strictEqual(missing.write.createCalls, 1);
    const routeCreate = missing.write.requests.filter(r => r.method === 'POST')[0];
    assert.strictEqual(routeCreate.name, 'route:render');
    const routeSpec = specs.find(spec => spec.name === 'route:render');
    assert.strictEqual(routeCreate.color, routeSpec.color);
    assert.strictEqual(routeCreate.description, routeSpec.description);
    assert.strictEqual(missing.write.deleteCalls, 0);
    assert.ok(before.every(name => name === 'route:render' || labels.includes(name)));
    const next = clients(labels);
    await provision(next.read, next.write, specs);
    assert.strictEqual(next.write.createCalls, 0);
    emit({
      inventory_labels: before.length,
      read_pages: read.listCalls,
      created_missing: ['route:render'],
      next_pass_creates: next.write.createCalls,
    });
  },

  async failures_fail_closed() {
    const labels = [...CANONICAL_LABELS, 'route:render', 'automation:ready'];
    const specs = buildSchedulerLabelSpecs(config());
    const cases = [
      ['primary-limit-403', Object.assign(new Error('rate limited'), {
        status: 403,
        response: {
          headers: {
            'x-ratelimit-remaining': '0',
            'x-ratelimit-reset': '1792721595',
          },
        },
      })],
      ['secondary-limit-429', Object.assign(new Error('too many requests'), { status: 429 })],
      ['server-500', Object.assign(new Error('server error'), { status: 500 })],
      ['timeout', Object.assign(new Error('operation timed out'), { code: 'ETIMEDOUT' })],
      ['permission-403', Object.assign(new Error('Resource not accessible'), { status: 403 })],
    ];
    const results = {};
    for (const [name, error] of cases) {
      const { read, write } = clients(labels, { listError: error });
      let message = '';
      try {
        await provision(read, write, specs);
        assert.fail(name + ' must fail closed');
      } catch (failure) {
        message = String(failure && failure.message || failure);
      }
      assert.strictEqual(write.createCalls, 0, name + ' must not fan out creates');
      assert.strictEqual(write.deleteCalls, 0);
      assert.ok(!message.includes('parent-owner'), name + ' must not leak owner');
      assert.ok(!message.includes('parent-repo'), name + ' must not leak repository');
      assert.ok(!message.includes('ghp_'), name + ' must not leak a token');
      results[name] = {
        creates: write.createCalls,
        reads: read.listCalls,
        diagnostic: message,
      };
    }

    // The next legitimate wake after the budget resets succeeds without any
    // manual repair and still creates only the genuinely missing label.
    labels.splice(labels.indexOf('automation:ready'), 1);
    const recovered = clients(labels);
    await provision(recovered.read, recovered.write, specs);
    assert.strictEqual(recovered.write.createCalls, 1);
    assert.strictEqual(
      recovered.write.requests.filter(r => r.method === 'POST')[0].name,
      'automation:ready'
    );
    emit({
      failures: results,
      recovered_creates: recovered.write.createCalls,
      recovered_reads: recovered.read.listCalls,
    });
  },

  async incomplete_inventory() {
    const labels = [...CANONICAL_LABELS];
    const specs = buildSchedulerLabelSpecs(config());
    const cases = {
      non_array: { data: { message: 'partial' } },
      invalid_entry: { data: [{ name: 'priority:p0' }, { name: '' }] },
    };
    const results = {};
    for (const [name, response] of Object.entries(cases)) {
      const { read, write } = clients(labels);
      read.rest.issues.listLabelsForRepo = async () => response;
      let message = '';
      try {
        await provision(read, write, specs);
        assert.fail(name + ' must fail closed');
      } catch (failure) {
        message = String(failure && failure.message || failure);
      }
      assert.strictEqual(write.createCalls, 0);
      results[name] = { creates: write.createCalls, diagnostic: message };
    }
    emit(results);
  },

  async spec_and_scope_contract() {
    const specs = buildSchedulerLabelSpecs(config());
    const names = specs.map(spec => spec.name);
    assert.deepStrictEqual(names, [
      ...CANONICAL_LABELS,
      'route:render',
      'automation:ready',
    ]);
    const priority = specs.filter(spec => spec.name.startsWith('priority:'));
    assert.deepStrictEqual(priority.map(spec => spec.name), [
      'priority:p0',
      'priority:p1',
      'priority:p2',
    ]);
    assert.deepStrictEqual(priority.map(spec => spec.color), [
      'B60205',
      'D93F0B',
      'FBCA04',
    ]);

    const defaults = buildSchedulerLabelSpecs({});
    assert.strictEqual(defaults.length, CANONICAL_LABELS.length);
    assert.ok(!defaults.some(spec => spec.name === 'route:render'));

    const labels = [];
    const { read, write } = clients(labels);
    // A route label may repeat a configured automation label; provisioning
    // must still send at most one POST for that name.
    const duplicate = buildSchedulerLabelSpecs(config({
      executionRouteLabels: new Set(['automation:in-progress', 'route:render']),
    }));
    await provision(read, write, duplicate, 'parent-owner', 'parent-repo');
    const inProgressCreates = write.requests.filter(
      r => r.method === 'POST' && r.name === 'automation:in-progress'
    );
    assert.strictEqual(inProgressCreates.length, 1);
    for (const request of [...read.requests, ...write.requests]) {
      assert.strictEqual(request.params.owner, 'parent-owner');
      assert.strictEqual(request.params.repo, 'parent-repo');
    }
    emit({
      names,
      default_count: defaults.length,
      in_progress_creates: inProgressCreates.length,
      owner_repo: 'parent-owner/parent-repo',
    });
  },

  async create_failure_fail_closed() {
    const labels = [...CANONICAL_LABELS];
    const specs = buildSchedulerLabelSpecs(config());
    const error = Object.assign(new Error('API rate limit exceeded'), {
      status: 403,
      response: { headers: { 'x-ratelimit-remaining': '0' } },
    });
    const { read, write } = clients(labels, { createError: error });
    let message = '';
    try {
      await provision(read, write, specs);
      assert.fail('a create failure must fail closed');
    } catch (failure) {
      message = String(failure && failure.message || failure);
    }
    assert.strictEqual(write.createCalls, 1);
    assert.strictEqual(write.deleteCalls, 0);
    assert.ok(message.includes('status=403'));
    assert.ok(!message.includes('parent-owner'));
    assert.ok(!message.includes('parent-repo'));
    emit({ creates: write.createCalls, diagnostic: message });
  },
};

(async () => {
  const name = process.argv[2];
  assert.ok(scenarios[name], 'unknown scenario: ' + name);
  await scenarios[name]();
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""


class SchedulerLabelProvisioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        assert cls.node is not None, (
            "node is required to execute the shipped scheduler JS; "
            "failing closed instead of silently skipping"
        )
        body = scheduler_body()
        cls.build_source = extract_function(body, "buildSchedulerLabelSpecs")
        cls.provision_source = extract_function(body, "provisionSchedulerLabels")

    def run_scenario(self, name: str) -> dict:
        script = "\n\n".join(
            [self.build_source, self.provision_source, HARNESS]
        )
        tmpdir = os.path.join(ROOT, ".opencode-tmp")
        os.makedirs(tmpdir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, dir=tmpdir
        ) as handle:
            handle.write(script)
            path = handle.name
        try:
            completed = subprocess.run(
                [self.node, path, name],
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            os.unlink(path)
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout,
        )
        lines = [
            line for line in completed.stdout.splitlines()
            if line.startswith("RESULT ")
        ]
        self.assertEqual(len(lines), 1, completed.stdout)
        return json.loads(lines[0][len("RESULT "):])

    def test_ten_no_change_passes_send_zero_label_posts(self):
        result = self.run_scenario("steady_state")
        self.assertEqual(result["passes"], 10)
        self.assertEqual(result["after"]["POST /labels"], 0)
        self.assertEqual(result["after"]["GET /labels"], 10)
        # Before/after is confined to this measured path: ten passes of nine
        # specs each used to send 90 POSTs through the shared PAT.
        self.assertEqual(result["before"]["POST /labels"], 90)
        self.assertEqual(result["before"]["credential"], "TAP_PAT")
        self.assertEqual(result["after"]["credential"], "GITHUB_TOKEN")

    def test_first_initialization_creates_only_missing_then_noops(self):
        result = self.run_scenario("first_initialization")
        self.assertEqual(result["first_pass"]["POST /labels"], 8)
        self.assertEqual(result["first_pass"]["GET /labels"], 1)
        self.assertEqual(result["second_pass"]["POST /labels"], 0)
        self.assertEqual(result["second_pass"]["GET /labels"], 1)
        self.assertNotIn("priority:p0", result["missing"])

    def test_concurrent_existing_label_race_stays_idempotent(self):
        result = self.run_scenario("concurrent_422")
        self.assertEqual(result["race_creates"], 1)
        self.assertEqual(result["second_pass_creates"], 0)

    def test_more_than_one_page_and_unrelated_labels_are_preserved(self):
        result = self.run_scenario("pagination_and_unrelated_labels")
        self.assertGreaterEqual(result["inventory_labels"], 150)
        self.assertGreaterEqual(result["read_pages"], 2)
        self.assertEqual(result["created_missing"], ["route:render"])
        self.assertEqual(result["next_pass_creates"], 0)

    def test_rate_limit_transient_and_permission_failures_do_not_create(self):
        result = self.run_scenario("failures_fail_closed")
        self.assertEqual(
            set(result["failures"]),
            {
                "primary-limit-403",
                "secondary-limit-429",
                "server-500",
                "timeout",
                "permission-403",
            },
        )
        for name, outcome in result["failures"].items():
            with self.subTest(failure=name):
                self.assertEqual(outcome["creates"], 0)
                self.assertTrue(outcome["diagnostic"])
        self.assertEqual(result["recovered_creates"], 1)

    def test_incomplete_inventory_fails_closed(self):
        result = self.run_scenario("incomplete_inventory")
        self.assertEqual(result["non_array"]["creates"], 0)
        self.assertEqual(result["invalid_entry"]["creates"], 0)

    def test_route_ready_priority_and_local_scope_are_preserved(self):
        result = self.run_scenario("spec_and_scope_contract")
        self.assertEqual(
            result["names"],
            CANONICAL_LABELS + ["route:render", "automation:ready"],
        )
        self.assertEqual(result["default_count"], len(CANONICAL_LABELS))
        self.assertEqual(result["in_progress_creates"], 1)
        self.assertEqual(result["owner_repo"], "parent-owner/parent-repo")

    def test_create_limit_failure_fails_closed_without_retry_storm(self):
        result = self.run_scenario("create_failure_fail_closed")
        self.assertEqual(result["creates"], 1)
        self.assertIn("status=403", result["diagnostic"])

    def test_workflow_wires_inventory_first_provisioning(self):
        body = scheduler_body()
        self.assertIn("async function provisionSchedulerLabels({", body)
        self.assertIn("readClient.paginate(", body)
        self.assertIn("readClient.rest.issues.listLabelsForRepo", body)
        self.assertIn("writeClient.rest.issues.createLabel({", body)
        self.assertIn("if (existingNames.has(spec.name)) continue;", body)
        self.assertIn("if (err && err.status === 422)", body)
        self.assertIn("readClient: readGithub,", body)
        self.assertIn("writeClient: github,", body)
        self.assertIn("repositoryOwner: owner,", body)
        self.assertIn("repositoryName: repo,", body)
        self.assertIn(
            "const labelSpecs = buildSchedulerLabelSpecs({",
            body,
        )
        # No unconditional per-pass create loop may survive the change.
        self.assertNotIn(
            "for (const spec of labelSpecs) {\n"
            "              try {\n"
            "                await github.rest.issues.createLabel({ owner, repo, ...spec });",
            body,
        )


if __name__ == "__main__":
    unittest.main()
