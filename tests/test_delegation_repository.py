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
args = sys.argv[1:]
endpoint = next((arg for arg in args if arg.startswith(('repos/', '/user/repos'))), '')
calls = os.environ.get('FIXTURE_CALLS', '')
if calls:
    with open(calls, 'a') as handle:
        handle.write(endpoint + '\\n')
fail = os.environ.get('GH_FAIL', '')
if fail and fail in endpoint:
    status = os.environ.get('GH_FAIL_STATUS', '')
    if status:
        print(f'gh: API error (HTTP {status})', file=sys.stderr)
    else:
        print('gh: API rate limit exceeded (HTTP 403)', file=sys.stderr)
    sys.exit(1)
malformed = os.environ.get('GH_MALFORMED', '')
if malformed and malformed in endpoint:
    print('not-json', end='')
    sys.exit(0)
if endpoint.startswith('/user/repos'):
    listing_pages = data.get('listing_pages')
    if listing_pages:
        selected = listing_pages if '--paginate' in args else listing_pages[:1]
        names = [name for page in selected for name in page]
    else:
        names = data.get('repositories', list(data['children']))
    print('\\n'.join(names))
elif '/actions/variables' in endpoint:
    repo = '/'.join(endpoint.split('/')[1:3])
    values = data['parent'] if repo == 'owner/parent' else data['children'].get(repo, {})
    variables = [{'name': k, 'value': v} for k,v in values.items()]
    # Simulate GitHub's 100-variable page size. The real resolver must ask gh
    # to paginate or relationship variables beyond page one disappear.
    if '--paginate' not in args:
        variables = variables[:100]
    print(json.dumps({'variables': variables}))
elif '/contents/.continuum.yml' in endpoint:
    repo = '/'.join(endpoint.split('/')[1:3])
    legacy = data.get('legacy_by_repository', {}).get(repo, data.get('legacy'))
    if legacy is None:
        sys.exit(1)
    print(base64.b64encode(legacy.encode()).decode())
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
                   PARENT_ROLE='', PARENT_CHILDREN='', CHILD_REPOSITORIES='',
                   CONTINUUM_DISCOVERY_BASE_DELAY='0')
        env.update(extra_env or {})
        return subprocess.run(['bash', str(ROOT / '.github/scripts/delegation_repository.sh'), *args],
                              env=env, cwd=self.root, text=True, capture_output=True)

    UNAVAILABLE = 'Delegated child discovery is unavailable; refusing to guess.'
    NO_UNIQUE = 'No unique repository declares the requested child relationship.'
    AMBIGUOUS = 'More than one repository declares the same child relationship.'
    NOT_ALLOWED = 'Child id is not allowed by CONTINUUM_CHILDREN.'

    def configure_children(self, child_ids, extra_candidates=0, listing=None):
        self.data['parent']['CONTINUUM_CHILDREN'] = json.dumps(child_ids)
        children = {
            f'owner/{child_id}-repo': {
                'CONTINUUM_ROLE': 'child',
                'CONTINUUM_CHILD_ID': child_id,
                'CONTINUUM_PARENT': 'owner/parent',
            }
            for child_id in child_ids
        }
        for index in range(extra_candidates):
            children[f'owner/unrelated-{index:03d}'] = {}
        self.data['children'] = children
        self.data['repositories'] = list(listing) if listing is not None else list(children)
        return children

    @staticmethod
    def variable_repositories(calls):
        repositories = []
        for endpoint in calls:
            if endpoint.startswith('repos/') and '/actions/variables' in endpoint:
                repositories.append(endpoint.split('/actions/variables', 1)[0][len('repos/'):])
        return repositories

    def test_plan_discovers_verified_child(self):
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [{'id': 'alpha', 'repository': 'owner/child'}])

    def test_repository_map_resolves_against_parent_variables_not_stale_file(self):
        result = self.run_resolver(
            'resolve',
            'alpha',
            extra_env={
                'CHILD_REPOSITORIES': json.dumps({'alpha': 'owner/child'}),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'owner/child')

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

    def test_unavailable_discovery_never_emits_an_empty_success(self):
        # kodmial/continuum#326: an API outage must never look like a
        # successful empty child inventory. The plan fails closed (exit 4)
        # with no payload so the scheduler reports a distinct degraded state
        # instead of fabricating `[]`.
        result = self.run_resolver('plan', extra_env={'GH_FAIL': '/user/repos'})
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        self.assertNotIn('[]', result.stdout)

    def test_rate_limited_discovery_retries_are_bounded(self):
        # A persistent rate limit retries a bounded number of times (never a
        # busy loop) and then fails closed. Default budget is 2 attempts.
        calls_file = self.root / 'calls.log'
        calls_file.write_text('')
        result = self.run_resolver(
            'plan',
            extra_env={'GH_FAIL': '/user/repos', 'FIXTURE_CALLS': str(calls_file)},
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        endpoints = calls_file.read_text().split()
        user_repo_calls = [e for e in endpoints if e.startswith('/user/repos')]
        self.assertEqual(len(user_repo_calls), 2, endpoints)

    def test_discovery_fallback_backoff_is_exponential(self):
        sleep_log = self.root / 'sleep.log'
        sleep_log.write_text('')
        sleep = self.root / 'sleep'
        sleep.write_text(
            '#!/usr/bin/env bash\n'
            'printf "%s\\n" "$1" >> "$SLEEP_LOG"\n'
        )
        sleep.chmod(0o755)
        result = self.run_resolver(
            'plan',
            extra_env={
                'GH_FAIL': '/user/repos',
                'CONTINUUM_DISCOVERY_MAX_ATTEMPTS': '3',
                'CONTINUUM_DISCOVERY_BASE_DELAY': '2',
                'SLEEP_LOG': str(sleep_log),
            },
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(sleep_log.read_text().split(), ['2', '4'])

    def test_trusted_inventory_is_reused_within_ttl_without_rescan(self):
        cache = self.root / 'inventory.json'
        calls_file = self.root / 'cache-calls.log'
        calls_file.write_text('')
        first = self.run_resolver(
            'plan',
            extra_env={
                'CONTINUUM_DELEGATION_CACHE': str(cache),
                'FIXTURE_CALLS': str(calls_file),
            },
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            json.loads(first.stdout),
            [{'id': 'alpha', 'repository': 'owner/child'}],
        )
        self.assertTrue(cache.exists())
        first_calls = calls_file.read_text().split()
        # Even with discovery now failing, a trusted unexpired inventory is
        # reused (authenticated + scoped + TTL-limited) with no second rescan.
        second = self.run_resolver(
            'plan',
            extra_env={
                'CONTINUUM_DELEGATION_CACHE': str(cache),
                'GH_FAIL': '/user/repos',
                'FIXTURE_CALLS': str(calls_file),
            },
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))
        second_calls = calls_file.read_text().split()
        self.assertNotIn('/user/repos', second_calls[len(first_calls):])


    def test_plan_role_discovery_request_counts_are_independent_of_child_count(self):
        # The role-based plan path must read one candidate inventory per plan
        # and probe each visible candidate once, regardless of how many child
        # ids the parent permits. R < C still fails closed, but never rescans.
        for child_count in (1, 2, 10):
            for repo_count in (1, 10, 100):
                with self.subTest(child_count=child_count, repo_count=repo_count):
                    self.data = {
                        'parent': {'CONTINUUM_ROLE': 'parent'},
                        'children': {},
                    }
                    child_ids = [f'child{index:02d}' for index in range(child_count)]
                    children = self.configure_children(
                        child_ids,
                        extra_candidates=max(0, repo_count - child_count),
                    )
                    if repo_count < child_count:
                        self.data['repositories'] = list(children)[:repo_count]
                    calls_file = self.root / 'calls.log'
                    calls_file.write_text('')
                    result = self.run_resolver(
                        'plan', extra_env={'FIXTURE_CALLS': str(calls_file)}
                    )
                    if repo_count >= child_count:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        plan = json.loads(result.stdout)
                        self.assertEqual([entry['id'] for entry in plan], child_ids)
                    else:
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertEqual(result.stdout.strip(), '')
                    calls = calls_file.read_text().split()
                    listing_calls = [call for call in calls if call.startswith('/user/repos')]
                    self.assertEqual(len(listing_calls), 1, calls)
                    variables = self.variable_repositories(calls)
                    candidate_variables = [
                        repo for repo in variables if repo != 'owner/parent'
                    ]
                    self.assertEqual(len(candidate_variables), repo_count, calls)
                    self.assertEqual(
                        len(set(candidate_variables)), repo_count,
                        'each candidate must be probed at most once per plan',
                    )

    def test_plan_preserves_allowlist_order_with_multiple_bindings(self):
        self.data['parent']['CONTINUUM_CHILDREN'] = '["beta","alpha"]'
        self.data['children'] = {
            'owner/alpha-repo': {
                'CONTINUUM_ROLE': 'child',
                'CONTINUUM_CHILD_ID': 'alpha',
                'CONTINUUM_PARENT': 'owner/parent',
            },
            'owner/beta-repo': {
                'CONTINUUM_ROLE': 'child',
                'CONTINUUM_CHILD_ID': 'beta',
                'CONTINUUM_PARENT': 'owner/parent',
            },
        }
        self.data['repositories'] = ['owner/beta-repo', 'owner/alpha-repo']
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                {'id': 'beta', 'repository': 'owner/beta-repo'},
                {'id': 'alpha', 'repository': 'owner/alpha-repo'},
            ],
        )

    def test_plan_ambiguous_later_child_never_returns_a_partial_plan(self):
        self.configure_children(['alpha', 'beta'])
        self.data['children']['owner/duplicate-beta'] = {
            'CONTINUUM_ROLE': 'child',
            'CONTINUUM_CHILD_ID': 'beta',
            'CONTINUUM_PARENT': 'owner/parent',
        }
        self.data['repositories'].append('owner/duplicate-beta')
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        self.assertIn(self.AMBIGUOUS, result.stderr)
        self.assertNotIn(self.UNAVAILABLE, result.stderr)

    def test_plan_rejects_a_child_claiming_another_parent(self):
        self.configure_children(['alpha'])
        self.data['children']['owner/alpha-repo']['CONTINUUM_PARENT'] = 'owner/other'
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        self.assertIn(self.NO_UNIQUE, result.stderr)
        self.assertNotIn('owner/alpha-repo', result.stdout + result.stderr)

    def test_plan_skips_the_parent_repository_candidate(self):
        self.configure_children(['alpha'])
        self.data['repositories'] = ['owner/parent', 'owner/alpha-repo']
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [{'id': 'alpha', 'repository': 'owner/alpha-repo'}],
        )

    def test_plan_resolves_a_legacy_only_candidate_once(self):
        self.configure_children(['alpha'])
        self.data['children']['owner/alpha-repo'] = {}
        self.data['legacy_by_repository'] = {
            'owner/alpha-repo': (
                'version: 1\ndelegation:\n'
                '  role: child\n  id: alpha\n  parent: owner/parent\n'
            )
        }
        calls_file = self.root / 'calls.log'
        calls_file.write_text('')
        result = self.run_resolver(
            'plan', extra_env={'FIXTURE_CALLS': str(calls_file)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [{'id': 'alpha', 'repository': 'owner/alpha-repo'}],
        )
        contents = [
            call for call in calls_file.read_text().split()
            if '/contents/.continuum.yml' in call
        ]
        self.assertEqual(len(contents), 1)

    def test_plan_consumes_every_listing_and_variable_page(self):
        parent = {f'DUMMY_PARENT_{index:03d}': 'x' for index in range(105)}
        parent.update({'CONTINUUM_ROLE': 'parent', 'CONTINUUM_CHILDREN': '["alpha"]'})
        child = {f'DUMMY_CHILD_{index:03d}': 'x' for index in range(105)}
        child.update({
            'CONTINUUM_ROLE': 'child',
            'CONTINUUM_CHILD_ID': 'alpha',
            'CONTINUUM_PARENT': 'owner/parent',
        })
        self.data['parent'] = parent
        self.data['children'] = {
            'owner/unrelated-000': {},
            'owner/alpha-repo': child,
        }
        self.data['listing_pages'] = [
            ['owner/unrelated-000'],
            ['owner/unrelated-000', 'owner/alpha-repo'],
        ]
        calls_file = self.root / 'calls.log'
        calls_file.write_text('')
        result = self.run_resolver(
            'plan', extra_env={'FIXTURE_CALLS': str(calls_file)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [{'id': 'alpha', 'repository': 'owner/alpha-repo'}],
        )
        calls = calls_file.read_text().split()
        self.assertEqual(len([call for call in calls if call.startswith('/user/repos')]), 1)
        self.assertEqual(
            len([call for call in calls if 'owner/alpha-repo/actions/variables' in call]),
            1,
        )

    def test_plan_fails_closed_on_429_candidate_lookup(self):
        result = self.run_resolver(
            'plan',
            extra_env={
                'GH_FAIL': 'repos/owner/child/actions/variables',
                'GH_FAIL_STATUS': '429',
            },
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        self.assertIn(self.UNAVAILABLE, result.stderr)
        self.assertNotIn('owner/child', result.stdout + result.stderr)

    def test_plan_fails_closed_on_malformed_candidate_variables(self):
        result = self.run_resolver(
            'plan',
            extra_env={'GH_MALFORMED': 'repos/owner/child/actions/variables'},
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        self.assertIn(self.UNAVAILABLE, result.stderr)
        self.assertNotIn('owner/child', result.stdout + result.stderr)

    def test_explicit_map_plan_never_reads_the_role_inventory(self):
        calls_file = self.root / 'calls.log'
        calls_file.write_text('')
        result = self.run_resolver(
            'plan',
            extra_env={
                'CHILD_REPOSITORIES': json.dumps({'alpha': 'owner/child'}),
                'FIXTURE_CALLS': str(calls_file),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [{'id': 'alpha', 'repository': 'owner/child'}],
        )
        self.assertNotIn(
            '/user/repos', calls_file.read_text().split()
        )

    def test_changed_binding_is_rejected_by_fresh_verify_not_plan_cache(self):
        first = self.run_resolver('plan')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.data['children']['owner/child']['CONTINUUM_PARENT'] = 'owner/other'
        verify = self.run_resolver('verify', 'alpha', 'owner/child')
        self.assertNotEqual(verify.returncode, 0)
        second = self.run_resolver('plan')
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertEqual(second.stdout.strip(), '')

    def test_role_and_explicit_map_plans_are_byte_identical(self):
        role_based = self.run_resolver('plan')
        explicit = self.run_resolver(
            'plan',
            extra_env={'CHILD_REPOSITORIES': json.dumps({'alpha': 'owner/child'})},
        )
        self.assertEqual(role_based.returncode, 0, role_based.stderr)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(role_based.stdout, explicit.stdout)

    def test_explicit_map_is_still_bounded_by_the_current_allowlist(self):
        self.data['parent']['CONTINUUM_CHILDREN'] = '["beta"]'
        result = self.run_resolver(
            'plan',
            extra_env={'CHILD_REPOSITORIES': json.dumps({'alpha': 'owner/child'})},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '')

    def test_plan_rejects_a_revoked_child_binding(self):
        self.configure_children(['alpha'])
        self.data['children']['owner/alpha-repo']['CONTINUUM_CHILD_ID'] = 'revoked'
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        self.assertIn(self.NO_UNIQUE, result.stderr)
        self.assertNotIn('owner/alpha-repo', result.stdout + result.stderr)
