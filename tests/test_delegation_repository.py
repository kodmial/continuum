"""Exercise repository discovery through the real shell resolver, offline."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = {
            'parent': {'CONTINUUM_ROLE': 'parent', 'CONTINUUM_CHILDREN': '["alpha"]'},
            'children': {'owner/child': {'CONTINUUM_ROLE': 'child', 'CONTINUUM_CHILD_ID': 'alpha', 'CONTINUUM_PARENT': 'owner/parent'}},
        }
        gh = self.root / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import base64, json, os, sys
from pathlib import Path
data = json.loads(Path(os.environ['FIXTURE']).read_text())
endpoint = next((arg for arg in sys.argv if arg.startswith(('repos/', '/user/repos'))), '')
calls = os.environ.get('FIXTURE_CALLS', '')
if calls:
    with open(calls, 'a') as handle:
        handle.write(endpoint + '\\n')
fail = os.environ.get('GH_FAIL', '')
if fail and fail in endpoint:
    print('gh: API rate limit exceeded (HTTP 403)', file=sys.stderr)
    sys.exit(1)
if endpoint.startswith('/user/repos'):
    print('\\n'.join(data['children']))
elif '/actions/variables' in endpoint:
    repo = endpoint.split('/')[1:3]
    values = data['parent'] if '/'.join(repo) == 'owner/parent' else data['children'].get('/'.join(repo), {})
    variables = [{'name': k, 'value': v} for k,v in values.items()]
    # Simulate GitHub's 100-variable page size. The real resolver must ask gh
    # to paginate or relationship variables beyond page one disappear.
    if '--paginate' not in sys.argv:
        variables = variables[:100]
    print(json.dumps({'variables': variables}))
elif '/contents/.continuum.yml' in endpoint and data.get('legacy'):
    print(base64.b64encode(data['legacy'].encode()).decode())
else:
    sys.exit(1)
''')
        gh.chmod(0o755)
        (self.root / '.continuum.yml').write_text('version: 1\n')

    def run_resolver(self, *args, extra_env=None):
        fixture = self.root / 'fixture.json'
        fixture.write_text(json.dumps(self.data))
        env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}",
                   FIXTURE=str(fixture), CONTINUUM_ENGINE_ROOT=str(ROOT),
                   GITHUB_REPOSITORY='owner/parent', RUNNER_TEMP=str(self.root),
                   PARENT_CONFIG=str(self.root / '.continuum.yml'),
                   PARENT_ROLE='', PARENT_CHILDREN='', CHILD_REPOSITORIES='')
        env.update(extra_env or {})
        return subprocess.run(['bash', str(ROOT / '.github/scripts/delegation_repository.sh'), *args],
                              env=env, cwd=self.root, text=True, capture_output=True)

    UNAVAILABLE = 'Delegated child discovery is unavailable; refusing to guess.'
    NO_UNIQUE = 'No unique repository declares the requested child relationship.'
    AMBIGUOUS = 'More than one repository declares the same child relationship.'
    NOT_ALLOWED = 'Child id is not allowed by CONTINUUM_CHILDREN.'

    def test_plan_discovers_verified_child(self):
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [{'id': 'alpha', 'repository': 'owner/child'}])

    def test_relationship_variables_beyond_first_page_are_discovered(self):
        parent = {f'DUMMY_PARENT_{i:03d}': 'x' for i in range(105)}
        parent.update({
            'CONTINUUM_ROLE': 'parent',
            'CONTINUUM_CHILDREN': '["alpha"]',
        })
        child = {f'DUMMY_CHILD_{i:03d}': 'x' for i in range(105)}
        child.update({
            'CONTINUUM_ROLE': 'child',
            'CONTINUUM_CHILD_ID': 'alpha',
            'CONTINUUM_PARENT': 'owner/parent',
        })
        self.data['parent'] = parent
        self.data['children']['owner/child'] = child

        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [{'id': 'alpha', 'repository': 'owner/child'}],
        )

    def test_invalid_parent_is_an_error_not_an_empty_success(self):
        self.data['parent']['CONTINUUM_ROLE'] = 'child'
        self.assertNotEqual(self.run_resolver('plan').returncode, 0)

    def test_ambiguous_child_is_rejected(self):
        self.data['children']['owner/duplicate'] = self.data['children']['owner/child']
        self.assertNotEqual(self.run_resolver('plan').returncode, 0)

    def test_missing_child_is_rejected(self):
        self.data['children'] = {}
        self.assertNotEqual(self.run_resolver('resolve', 'alpha').returncode, 0)

    def test_child_outside_parent_allowlist_is_rejected(self):
        self.assertNotEqual(self.run_resolver('verify', 'beta', 'owner/child').returncode, 0)

    def test_explicit_wrong_parent_cannot_use_legacy_fallback(self):
        self.data['children']['owner/child']['CONTINUUM_PARENT'] = 'owner/other'
        self.data['legacy'] = 'version: 1\ndelegation:\n  role: child\n  id: alpha\n  parent: owner/parent\n'
        self.assertNotEqual(self.run_resolver('verify', 'alpha', 'owner/child').returncode, 0)

    def test_legacy_child_without_variables_remains_compatible(self):
        self.data['children']['owner/child'] = {}
        self.data['legacy'] = 'version: 1\ndelegation:\n  role: child\n  id: alpha\n  parent: owner/parent\n'
        result = self.run_resolver('verify', 'alpha', 'owner/child')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_validation_path_is_checked(self):
        self.data['children']['owner/child']['CONTINUUM_VALIDATION_SCRIPT'] = '../escape.sh'
        self.assertNotEqual(self.run_resolver('validation', 'owner/child').returncode, 0)
        self.data['children']['owner/child']['CONTINUUM_VALIDATION_SCRIPT'] = 'scripts/validate.sh'
        result = self.run_resolver('validation', 'owner/child')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'scripts/validate.sh')

    def test_post_completion_reentry_resolves_the_same_child(self):
        # Scheduler re-entry after task/review/merge/cleanup must resolve the
        # same child deterministically. Completion artifacts (closed issues,
        # merged PRs, deleted runs) are not resolver inputs, so two
        # consecutive wakes observe the same binding.
        first = self.run_resolver('plan')
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_resolver('plan')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))
        self.assertEqual(
            json.loads(second.stdout),
            [{'id': 'alpha', 'repository': 'owner/child'}],
        )

    def test_resolver_never_consults_completion_artifacts(self):
        # The resolver may read relationship variables, the repository
        # listing, and legacy relationship files only. If it ever paginated
        # issues, pull requests, or workflow runs, a completion storm that
        # exhausts the API budget could change the resolution verdict.
        calls_file = self.root / 'calls.log'
        calls_file.write_text('')
        result = self.run_resolver('plan', extra_env={'FIXTURE_CALLS': str(calls_file)})
        self.assertEqual(result.returncode, 0, result.stderr)
        endpoints = calls_file.read_text().split()
        self.assertTrue(endpoints, 'the resolver must call the API to discover the child')
        for endpoint in endpoints:
            self.assertNotIn('/issues', endpoint)
            self.assertNotIn('/pulls', endpoint)
            self.assertNotIn('/actions/runs', endpoint)

    def test_rate_limited_child_lookup_is_unavailable_not_zero_match(self):
        # kodmial/continuum#171: a 403 while reading the child during a
        # post-completion wake storm must not be reported as "no repository
        # declares". An unrelated non-matching candidate is listed first to
        # prove ordinary non-matches still continue the scan.
        self.data['children'] = {
            'owner/unrelated': {'CONTINUUM_ROLE': 'none'},
            'owner/child': self.data['children']['owner/child'],
        }
        result = self.run_resolver(
            'plan', extra_env={'GH_FAIL': 'repos/owner/child/actions/variables'}
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(self.UNAVAILABLE, result.stderr)
        self.assertNotIn(self.NO_UNIQUE, result.stderr)
        self.assertNotIn('owner/child', result.stdout + result.stderr)

    def test_rate_limited_enumeration_is_unavailable_not_zero_match(self):
        result = self.run_resolver('plan', extra_env={'GH_FAIL': '/user/repos'})
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(self.UNAVAILABLE, result.stderr)
        self.assertNotIn(self.NO_UNIQUE, result.stderr)

    def test_unreadable_parent_lookup_is_unavailable_not_allowlist_rejection(self):
        result = self.run_resolver(
            'plan', extra_env={'GH_FAIL': 'repos/owner/parent/actions/variables'}
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(self.UNAVAILABLE, result.stderr)
        self.assertNotIn(self.NOT_ALLOWED, result.stderr)
        self.assertNotIn(self.NO_UNIQUE, result.stderr)

    def test_verify_reports_unavailable_when_child_api_fails(self):
        result = self.run_resolver(
            'verify', 'alpha', 'owner/child',
            extra_env={'GH_FAIL': 'repos/owner/child/actions/variables'},
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(self.UNAVAILABLE, result.stderr)

    def test_validation_reports_unavailable_when_child_api_fails(self):
        result = self.run_resolver(
            'validation', 'owner/child',
            extra_env={'GH_FAIL': 'repos/owner/child/actions/variables'},
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(self.UNAVAILABLE, result.stderr)

    def test_genuine_zero_match_keeps_fail_closed_message(self):
        self.data['children'] = {}
        result = self.run_resolver('resolve', 'alpha')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(self.NO_UNIQUE, result.stderr)
        self.assertNotIn(self.UNAVAILABLE, result.stderr)

    def test_genuine_ambiguity_keeps_fail_closed_message(self):
        self.data['children']['owner/duplicate'] = self.data['children']['owner/child']
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(self.AMBIGUOUS, result.stderr)
        self.assertNotIn(self.UNAVAILABLE, result.stderr)

    def test_allowlist_rejection_message_is_preserved(self):
        result = self.run_resolver('verify', 'beta', 'owner/child')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(self.NOT_ALLOWED, result.stderr)
        self.assertNotIn(self.UNAVAILABLE, result.stderr)

    def test_failures_never_name_the_private_child(self):
        # The opaque mapping architecture: error verdicts must not disclose
        # the private child repository identity in public parent logs.
        self.data['children']['owner/child']['CONTINUUM_PARENT'] = 'owner/other'
        mismatch = self.run_resolver('verify', 'alpha', 'owner/child')
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertNotIn('owner/child', mismatch.stdout + mismatch.stderr)
        outage = self.run_resolver(
            'plan', extra_env={'GH_FAIL': 'repos/owner/child/actions/variables'}
        )
        self.assertEqual(outage.returncode, 4, outage.stderr)
        self.assertNotIn('owner/child', outage.stdout + outage.stderr)
