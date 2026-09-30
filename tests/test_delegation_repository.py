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
if endpoint.startswith('/user/repos'):
    print('\\n'.join(data['children']))
elif '/actions/variables' in endpoint:
    repo = endpoint.split('/')[1:3]
    values = data['parent'] if '/'.join(repo) == 'owner/parent' else data['children'].get('/'.join(repo), {})
    print(json.dumps({'variables': [{'name': k, 'value': v} for k,v in values.items()]}))
elif '/contents/.continuum.yml' in endpoint and data.get('legacy'):
    print(base64.b64encode(data['legacy'].encode()).decode())
else:
    sys.exit(1)
''')
        gh.chmod(0o755)
        (self.root / '.continuum.yml').write_text('version: 1\n')

    def run_resolver(self, *args):
        fixture = self.root / 'fixture.json'
        fixture.write_text(json.dumps(self.data))
        env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}",
                   FIXTURE=str(fixture), CONTINUUM_ENGINE_ROOT=str(ROOT),
                   GITHUB_REPOSITORY='owner/parent', RUNNER_TEMP=str(self.root),
                   PARENT_CONFIG=str(self.root / '.continuum.yml'),
                   PARENT_ROLE='', PARENT_CHILDREN='', CHILD_REPOSITORIES='')
        return subprocess.run(['bash', str(ROOT / '.github/scripts/delegation_repository.sh'), *args],
                              env=env, cwd=self.root, text=True, capture_output=True)

    def test_plan_discovers_verified_child(self):
        result = self.run_resolver('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [{'id': 'alpha', 'repository': 'owner/child'}])

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
