"""Deterministic coverage for kodmial/continuum#179 (spec section 12).

Each test maps to one numbered requirement: immutable digests, zero
bootstrap install on the warm path, fresh instance per demand, zero idle,
single use, terminal-path teardown, JIT failure, restart + reconciler,
retry + reconciliation, stale registration cleanup, per-job network
lifecycle, IP-equality tolerance, consumer isolation, fork-cache guard,
shared canonical contract across Linux/macOS (+ qualified Windows), and
pinned-ref stability.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from continuum import agent_runtime as runtime


def _controller(project="proj-a"):
    store = runtime.ImageStore(project_id=project)
    provider = runtime.FakeProvider()
    return runtime.EphemeralController(project_id=project, image_store=store, provider=provider)


def _manifest(profile=None, **overrides):
    os_name = profile.os if profile is not None else overrides.pop("os", "linux")
    arch = profile.arch if profile is not None else overrides.pop("arch", "x64")
    ref = profile.continuum_ref if profile is not None else overrides.pop("continuum_ref", "main")
    toolchain = profile.toolchain if profile is not None else overrides.pop("toolchain", ())
    return runtime.canonical_manifest(os_name, arch, ref, toolchain)


class AgentRuntimeContractTest(unittest.TestCase):
    def test_01_same_manifest_profile_same_digest(self):
        manifest_a = runtime.canonical_manifest("linux", "x64", "main")
        manifest_b = runtime.canonical_manifest("linux", "x64", "main")
        profile_a = runtime.resolve_profile({"preset": "agent-linux"})
        profile_b = runtime.resolve_profile({"preset": "agent-linux"})
        self.assertEqual(runtime.image_digest(manifest_a, profile_a),
                         runtime.image_digest(manifest_b, profile_b))

    def test_02_changed_runtime_toolchain_input_new_digest(self):
        manifest_a = runtime.canonical_manifest("linux", "x64", "main")
        profile_a = runtime.resolve_profile({"preset": "agent-linux"})
        base = runtime.image_digest(manifest_a, profile_a)
        changed_toolchain = runtime.canonical_manifest("linux", "x64", "main", ("go-1.23",))
        profile_changed = runtime.resolve_profile({"preset": "agent-linux", "toolchain": ["go-1.23"]})
        self.assertNotEqual(base, runtime.image_digest(changed_toolchain, profile_a))
        self.assertNotEqual(base, runtime.image_digest(manifest_a, profile_changed))
        changed_opencode = runtime.AgentManifest(**{**manifest_a.to_canonical(), "opencode_version": "9.9.9"})
        self.assertNotEqual(base, runtime.image_digest(changed_opencode, profile_a))

    def test_03_normal_execution_contains_no_bootstrap_install(self):
        # Hermetic warm-path contract: inline fixtures exercise the
        # detectors without a hard dependency on checked-in workflow files.
        warm_body = (
            "        env:\n"
            "          CONTINUUM_IMAGE_DIGEST: ${{ vars.CONTINUUM_IMAGE_DIGEST }}\n"
            "        run: |\n"
            "          # Continuum prepared agent runtime: the immutable golden image\n"
            "          # (or its provider-native cache equivalent) already carries pinned\n"
            "          # OpenCode 1.18.34. The warm hit below is keyed by the image digest\n"
            "          # in CONTINUUM_IMAGE_DIGEST (wired from the repository variable).\n"
            '          export PATH="$HOME/.opencode/bin:$PATH"\n'
            '          if [[ "${CONTINUUM_IMAGE_DIGEST:-}" =~ ^[0-9a-f]{64}$ ]]'
            " && command -v opencode >/dev/null 2>&1"
            ' && opencode --version 2>&1 | grep -E -q "(^|[^0-9.])1\\.18\\.34([^0-9.]|$)"; then\n'
            '            echo "prepared-runtime hit: opencode 1.18.34 already present'
            ' (image digest ${CONTINUUM_IMAGE_DIGEST})."\n'
            '            echo "$HOME/.opencode/bin" >> "$GITHUB_PATH"\n'
            "            exit 0\n"
            "          fi\n"
            "          curl -fsSL --retry 3 https://opencode.ai/install | bash\n"
        )
        self.assertFalse(
            runtime.normal_execution_uses_bootstrap_install(warm_body),
            "warm path must probe the prepared runtime, not bootstrap-install",
        )
        self.assertTrue(
            runtime.workflow_step_has_prepared_runtime_probe(warm_body),
            "prepared-runtime probe with pinned versions + image digest required",
        )
        # The Python detector alone is not zero-download proof: the
        # required evidence is the Ruby exit-0/else contract (the warm hit
        # short-circuits before any download). The fixture must carry it.
        hit_at = warm_body.index("prepared-runtime hit")
        installer_at = warm_body.index("https://opencode.ai/install")
        exit_at = warm_body.index("exit 0")
        self.assertLess(hit_at, installer_at)
        self.assertLess(hit_at, exit_at)
        self.assertLess(exit_at, installer_at)
        # A probe before the installer without a validated cache-miss guard
        # is not a proven warm path: removing `exit 0` while leaving the
        # probe means every run reinstalls, so the detector must report a
        # bootstrap install even though a probe precedes the installer.
        no_short_circuit = warm_body.replace("            exit 0\n", "")
        self.assertTrue(
            runtime.normal_execution_uses_bootstrap_install(no_short_circuit),
            "probe without a cache-miss guard (no exit 0/else) still reinstalls every run",
        )
        stripped_lines = no_short_circuit.splitlines()
        hit_line = next(i for i, line in enumerate(stripped_lines) if "prepared-runtime hit" in line)
        installer_line = next(i for i, line in enumerate(stripped_lines) if "https://opencode.ai/install" in line)
        short_circuited = any(
            stripped_lines[i].strip() == "exit 0" or stripped_lines[i].strip() == "else"
            for i in range(hit_line, installer_line)
        )
        self.assertFalse(
            short_circuited,
            "without exit 0 (or else) between hit and installer the Ruby short-circuit contract fails",
        )
        cold_body = (
            "        run: |\n"
            "          curl -fsSL --retry 3 https://opencode.ai/install | bash\n"
        )
        self.assertTrue(runtime.normal_execution_uses_bootstrap_install(cold_body))
        self.assertFalse(runtime.workflow_step_has_prepared_runtime_probe(cold_body))

    def test_03_integration_workflow_warm_path(self):
        # Separate integration check over the checked-in workflows. Missing
        # files skip instead of raising FileNotFoundError so the unit suite
        # never fails for reasons outside this module.
        names = ("continuum-opencode.yml", "continuum-pr-agent.yml",
                 "continuum-pr-agent-repair.yml")
        checked = 0
        for name in names:
            path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            with self.subTest(workflow=name):
                self.assertFalse(runtime.normal_execution_uses_bootstrap_install(body),
                                 "{}: warm path must probe the prepared runtime, not bootstrap-install".format(name))
                self.assertTrue(runtime.workflow_step_has_prepared_runtime_probe(body),
                                "{}: prepared-runtime probe with pinned versions + image digest required".format(name))
            checked += 1
        if checked == 0:
            self.fail("no workflow files present; checked-in warm-path workflows must exist")

    def test_04_queued_demand_creates_fresh_instance(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        self.assertEqual(controller.live_instance_count(), 0)
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(manifest, now=1000.0)
        self.assertEqual(result.outcome, "success")
        self.assertTrue(result.instance_id)
        self.assertIn("created-instance", " ".join(result.events))
        self.assertIn("after demand", " ".join(result.events))

    def test_05_no_demand_leaves_zero_live_instances(self):
        controller = _controller()
        self.assertEqual(controller.live_instance_count(), 0)
        self.assertEqual(controller.live_idle_count(), 0)
        with self.assertRaises(runtime.AgentRuntimeError):
            controller.run_next_job(_manifest(), now=1000.0)
        self.assertEqual(controller.live_instance_count(), 0)

    def test_06_one_instance_cannot_accept_second_job(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(_manifest(profile), now=1000.0)
        with self.assertRaises(runtime.AgentRuntimeError):
            controller.run_second_job_on_same_instance(result.instance_id, now=1060.0)

    def test_07_terminal_success_destroys_instance(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(_manifest(profile), now=1000.0, outcome="success")
        self.assertTrue(result.destroyed)
        self.assertEqual(controller.live_instance_count(), 0)
        instance = controller.provider.instances[result.instance_id]
        network = controller.provider.networks[result.network_id]
        self.assertIsNotNone(instance.destroyed_at)
        self.assertIsNotNone(network.destroyed_at)
        self.assertTrue(instance.log_forwarded)

    def test_08_failure_destroys_instance(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(_manifest(profile), now=1000.0, outcome="failure")
        self.assertEqual(result.outcome, "failure")
        self.assertTrue(result.destroyed)
        self.assertEqual(controller.live_instance_count(), 0)

    def test_09_cancellation_destroys_instance(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(_manifest(profile), now=1000.0, outcome="cancelled")
        self.assertEqual(result.outcome, "cancelled")
        self.assertTrue(result.destroyed)
        self.assertEqual(controller.live_instance_count(), 0)

    def test_10_jit_registration_failure_destroys_instance(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(_manifest(profile), now=1000.0, outcome="jit-failure")
        self.assertEqual(result.outcome, "jit-failure")
        self.assertTrue(result.destroyed)
        self.assertEqual(controller.live_instance_count(), 0)
        self.assertNotIn("jit-{}".format(result.instance_id), controller.provider.registrations.values())

    def test_11_restart_plus_orphan_lease_swept_by_reconciler(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        controller.queue_job("acme/app", profile, run_id="r1")
        result = controller.run_next_job(manifest, now=1000.0)
        self.assertTrue(result.destroyed)
        # Simulate a lost completion event: instance + lease survive teardown.
        store = controller.images
        provider = runtime.FakeProvider()
        revived = runtime.EphemeralController(project_id="proj-a", image_store=store, provider=provider)
        digest = runtime.image_digest(manifest, profile)
        store.ensure_image(manifest, profile, now=2000.0)
        network = provider.create_network(runtime.profile_digest(profile), "job-orphan", now=2000.0)
        orphan = provider.create_instance(
            project_id="proj-a", repository="acme/app",
            profile_digest=runtime.profile_digest(profile), digest=digest,
            job_id="job-orphan", network_id=network.id, now=2000.0)
        provider.jit_register(orphan)
        revived.leases[orphan.id] = runtime.Lease(
            project_id="proj-a", repository="acme/app", job_id="job-orphan", run_id="r9",
            profile_digest=runtime.profile_digest(profile), instance_id=orphan.id,
            created_at=2000.0, lease_expires_at=2000.0 + revived.max_job_lifetime,
            max_age_at=2000.0 + revived.global_max_age, state="running")
        restarted = revived.restart()
        removed = restarted.sweep_orphans(now=2000.0 + restarted.max_job_lifetime + 1.0)
        self.assertIn(orphan.id, removed)
        self.assertEqual(restarted.live_instance_count(), 0)

    def test_12_teardown_transient_failure_retries_then_reconciles(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.provider.destroy_failures_remaining = 99
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(_manifest(profile), now=1000.0, teardown_retries=2)
        self.assertFalse(result.destroyed)
        self.assertEqual(controller.live_instance_count(), 1)
        controller.provider.destroy_failures_remaining = 0
        removed = controller.sweep_orphans(now=1000.0 + controller.global_max_age + 1.0)
        self.assertIn(result.instance_id, removed)
        self.assertEqual(controller.live_instance_count(), 0)

    def test_13_stale_jit_registration_is_removed(self):
        controller = _controller()
        controller.provider.registrations["jit-ghost"] = "i-000000"
        removed = controller.sweep_orphans(now=5000.0)
        self.assertIn("jit-ghost", removed)
        self.assertNotIn("jit-ghost", controller.provider.registrations)

    def test_14_per_job_network_not_reused(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.queue_job("acme/app", profile)
        first = controller.run_next_job(_manifest(profile), now=1000.0)
        controller.queue_job("acme/app", profile)
        second = controller.run_next_job(_manifest(profile), now=2000.0)
        self.assertNotEqual(first.network_id, second.network_id)
        first_net = controller.provider.networks[first.network_id]
        second_net = controller.provider.networks[second.network_id]
        self.assertIsNotNone(first_net.destroyed_at)
        self.assertIsNotNone(second_net.destroyed_at)
        self.assertFalse(first_net.persistent)
        self.assertFalse(second_net.persistent)
        self.assertEqual(controller.provider.persistent_egress_objects, [])

    def test_15_ip_equality_is_not_failure(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        controller.queue_job("acme/app", profile)
        first = controller.run_next_job(_manifest(profile), now=1000.0)
        for _ in range(2):
            controller.queue_job("acme/app", profile)
            controller.run_next_job(_manifest(profile), now=2000.0)
        controller.queue_job("acme/app", profile)
        fourth = controller.run_next_job(_manifest(profile), now=3000.0)
        # Pool of 3 IPs: the 4th job reuses the first job's address string.
        self.assertEqual(first.public_ip, fourth.public_ip)
        self.assertNotEqual(first.instance_id, fourth.instance_id)
        # Lifecycle (fresh instance + destroyed network), not string
        # inequality, is the requirement: reissued IPs still pass.
        self.assertEqual(controller.live_instance_count(), 0)

    def test_16_unrelated_projects_do_not_share_state(self):
        store_a = runtime.ImageStore(project_id="proj-a")
        store_b = runtime.ImageStore(project_id="proj-b")
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        gen_a = store_a.ensure_image(manifest, profile, now=1000.0)
        gen_b = store_b.ensure_image(manifest, profile, now=1000.0)
        self.assertEqual(gen_a.digest, gen_b.digest)  # generic base reused by digest
        store_a.promote(gen_a.digest, now=1000.0)
        self.assertIsNone(store_b.active_digest)  # pointers are project-scoped
        cache = runtime.DependencyCache()
        key = runtime.DependencyCache.cache_key(gen_a.digest)
        cache.publish(key=key, project_id="proj-a", repository="acme/a",
                      payload={"files": ["x"]}, is_fork=False, trusted_context=True)
        self.assertIsNone(cache.restore(key=key, project_id="proj-b", repository="acme/b"))
        kept = cache.restore(key=key, project_id="proj-a", repository="acme/a")
        self.assertIsNotNone(kept)

    def test_17_untrusted_pr_cannot_publish_trusted_cache(self):
        cache = runtime.DependencyCache()
        key = runtime.DependencyCache.cache_key("a" * 64)
        entry = cache.publish(key=key, project_id="proj-a", repository="acme/app",
                              payload={"files": ["x"]}, is_fork=True, trusted_context=True)
        self.assertFalse(entry.trusted)
        with self.assertRaises(runtime.AgentRuntimeError):
            cache.publish(key=key, project_id="proj-a", repository="acme/app",
                          payload={"token": "ghp_secret"}, is_fork=False, trusted_context=True)

    def test_17b_untrusted_cache_restore_refused(self):
        cache = runtime.DependencyCache()
        key = runtime.DependencyCache.cache_key("b" * 64)
        cache.publish(key=key, project_id="proj-a", repository="acme/app",
                      payload={"files": ["x"]}, is_fork=True, trusted_context=True)
        # Fork-published entries are never usable: restore refuses them so a
        # caller checking only non-None cannot consume poisoned cache, and
        # the job falls back to deterministic reconstruction.
        self.assertIsNone(cache.restore(key=key, project_id="proj-a", repository="acme/app"))

    def test_17c_provisioning_path_restores_and_publishes_dependency_cache(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        controller.queue_job("acme/app", profile)
        first = controller.run_next_job(manifest, now=1000.0)
        self.assertIn("cache-miss", " ".join(first.events))
        self.assertIn("published-dependencies", " ".join(first.events))
        key = runtime.DependencyCache.cache_key(first.image_digest)
        kept = controller.cache.restore(key=key, project_id="proj-a", repository="acme/app")
        self.assertIsNotNone(kept)
        self.assertTrue(kept.validated)
        controller.queue_job("acme/app", profile)
        second = controller.run_next_job(manifest, now=2000.0)
        self.assertIn("restored-dependencies", " ".join(second.events))

    def test_17d_fork_job_never_leaves_usable_cache(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        controller.queue_job("acme/app", profile)
        result = controller.run_next_job(manifest, now=1000.0, is_fork=True)
        self.assertEqual(result.outcome, "success")
        key = runtime.DependencyCache.cache_key(result.image_digest)
        self.assertIsNone(controller.cache.restore(key=key, project_id="proj-a",
                                                   repository="acme/app"))

    def test_17f_untrusted_publish_never_clobbers_trusted_entry(self):
        cache = runtime.DependencyCache()
        key = runtime.DependencyCache.cache_key("c" * 64)
        cache.publish(key=key, project_id="proj-a", repository="acme/app",
                      payload={"files": ["trusted"]}, is_fork=False, trusted_context=True)
        fork_entry = cache.publish(key=key, project_id="proj-a", repository="acme/app",
                                   payload={"files": ["fork"]}, is_fork=True, trusted_context=True)
        self.assertFalse(fork_entry.trusted)
        kept = cache.restore(key=key, project_id="proj-a", repository="acme/app")
        self.assertIsNotNone(kept)
        self.assertTrue(kept.trusted)
        self.assertEqual(kept.payload, {"files": ["trusted"]})
        # Same through the provisioning path: a trusted success followed by
        # a fork success for the same digest keeps the trusted hit.
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        controller.queue_job("acme/app", profile)
        first = controller.run_next_job(manifest, now=1000.0)
        self.assertIn("published-dependencies", " ".join(first.events))
        controller.queue_job("acme/app", profile)
        forked = controller.run_next_job(manifest, now=2000.0, is_fork=True)
        self.assertIn("cache-publish-refused", " ".join(forked.events))
        digest_key = runtime.DependencyCache.cache_key(first.image_digest)
        surviving = controller.cache.restore(key=digest_key, project_id="proj-a",
                                             repository="acme/app")
        self.assertIsNotNone(surviving)
        self.assertTrue(surviving.trusted)

    def test_17e_concurrency_limit_bounds_live_provisioning(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux", "concurrency_limit": 1})
        manifest = _manifest(profile)
        store = controller.images
        provider = controller.provider
        digest = runtime.image_digest(manifest, profile)
        store.ensure_image(manifest, profile, now=1000.0)
        network = provider.create_network(runtime.profile_digest(profile), "job-live", now=1000.0)
        live = provider.create_instance(
            project_id="proj-a", repository="acme/app",
            profile_digest=runtime.profile_digest(profile), digest=digest,
            job_id="job-live", network_id=network.id, now=1000.0)
        provider.jit_register(live)
        controller.leases[live.id] = runtime.Lease(
            project_id="proj-a", repository="acme/app", job_id="job-live", run_id="r-live",
            profile_digest=runtime.profile_digest(profile), instance_id=live.id,
            created_at=1000.0, lease_expires_at=1000.0 + controller.max_job_lifetime,
            max_age_at=1000.0 + controller.global_max_age, state="running")
        controller.queue_job("acme/app", profile)
        with self.assertRaises(runtime.AgentRuntimeError):
            controller.run_next_job(manifest, now=1100.0)
        # Refused demand is kept, not dropped; the live instance is untouched.
        self.assertEqual(len(controller.queued), 1)
        self.assertEqual(controller.live_instance_count(), 1)

    def test_18_linux_and_macos_derive_from_same_contract(self):
        linux = runtime.LinuxAdapter().manifest("main")
        macos = runtime.MacOSAdapter().manifest("main")
        self.assertEqual(linux.opencode_version, macos.opencode_version)
        self.assertEqual(linux.pr_agent_version, macos.pr_agent_version)
        self.assertEqual(linux.runner_version, macos.runner_version)
        self.assertEqual(linux.schema_version, macos.schema_version)
        self.assertNotEqual(linux.base_image, macos.base_image)
        self.assertNotEqual(runtime.manifest_digest(linux), runtime.manifest_digest(macos))
        for adapter in (runtime.LinuxAdapter(), runtime.MacOSAdapter()):
            result = adapter.qualify("main")
            self.assertTrue(result["destroyed"])
            self.assertEqual(result["live_after"], 0)

    def test_19_advertised_windows_profile_is_qualified(self):
        platforms = {item["os"] for item in runtime.advertised_platforms()}
        self.assertIn("windows", platforms)
        result = runtime.WindowsAdapter().qualify("main")
        self.assertTrue(result["destroyed"])
        self.assertEqual(result["live_after"], 0)
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.canonical_manifest("plan9", "x64", "main")

    def test_20_pinned_ref_is_not_silently_mutated(self):
        old_manifest = runtime.canonical_manifest("linux", "x64", "v1.0.0")
        old_profile = runtime.resolve_profile({"preset": "agent-linux", "continuum_ref": "v1.0.0"})
        new_manifest = runtime.canonical_manifest("linux", "x64", "main")
        new_profile = runtime.resolve_profile({"preset": "agent-linux", "continuum_ref": "main"})
        self.assertNotEqual(runtime.image_digest(old_manifest, old_profile),
                            runtime.image_digest(new_manifest, new_profile))
        store = runtime.ImageStore(project_id="proj-a")
        gen = store.ensure_image(old_manifest, old_profile, now=1000.0)
        store.promote(gen.digest, now=1000.0)
        self.assertEqual(store.active_digest, gen.digest)
        new_gen = store.ensure_image(new_manifest, new_profile, now=2000.0)
        # Promoting the new ref never rewrites the old generation: rollback
        # is a pointer change back to the still-stored immutable digest.
        store.promote(new_gen.digest, now=2000.0)
        store.rollback(gen.digest, now=3000.0)
        self.assertEqual(store.active_digest_for(old_profile), gen.digest)

    def test_strict_invariants_reject_persistent_runners(self):
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux", "idle_instances": 1})
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux", "max_uses_per_instance": 2})
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux", "lifecycle": "persistent"})
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux",
                                     "network": {"lifecycle": "shared"}})

    def test_live_qualification_harness_passes(self):
        evidence = runtime.live_qualification_evidence(
            project_id="proj-qual", repository="acme/app", os="linux", arch="x64")
        self.assertTrue(evidence["pass"], evidence["checks"])
        self.assertTrue(evidence["different_instance"])
        self.assertTrue(evidence["ip_equality_is_not_failure"])

    def test_13b_orphan_network_without_instance_is_reclaimed(self):
        # A crash between create_network and create_instance leaves a
        # per-job attachment with no owning instance. The reconciler must
        # reclaim it once past grace, and leave fresh ones alone.
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        orphan_net = controller.provider.create_network(
            runtime.profile_digest(profile), "job-lost", now=1000.0)
        fresh_net = controller.provider.create_network(
            runtime.profile_digest(profile), "job-fresh", now=1500.0)
        removed = controller.sweep_orphans(now=1000.0 + controller.orphan_grace)
        self.assertIn(orphan_net.id, removed)
        self.assertIsNotNone(controller.provider.networks[orphan_net.id].destroyed_at)
        self.assertIsNone(controller.provider.networks[fresh_net.id].destroyed_at)
        self.assertNotIn(fresh_net.id, removed)

    def test_13c_attached_network_is_not_swept_as_orphan(self):
        # A network backing a live instance is owned: the network-only
        # sweep must leave it alone even past grace.
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        provider = controller.provider
        controller.images.ensure_image(manifest, profile, now=1000.0)
        network = provider.create_network(runtime.profile_digest(profile), "job-held", now=1000.0)
        live = provider.create_instance(
            project_id="proj-a", repository="acme/app",
            profile_digest=runtime.profile_digest(profile),
            digest=runtime.image_digest(manifest, profile),
            job_id="job-held", network_id=network.id, now=1000.0)
        provider.jit_register(live)
        controller.leases[live.id] = runtime.Lease(
            project_id="proj-a", repository="acme/app", job_id="job-held", run_id="r-held",
            profile_digest=runtime.profile_digest(profile), instance_id=live.id,
            created_at=1000.0, lease_expires_at=1000.0 + controller.max_job_lifetime,
            max_age_at=1000.0 + controller.global_max_age, state="running")
        removed = controller.sweep_orphans(now=1000.0 + controller.orphan_grace + 1.0)
        self.assertNotIn(network.id, removed)
        self.assertIsNone(provider.networks[network.id].destroyed_at)
        self.assertEqual(controller.live_instance_count(), 1)

    def test_profile_toolchain_validation_matches_manifest(self):
        # resolve_profile must enforce the same toolchain rules as
        # canonical_manifest: empty, oversize, newline/NUL, duplicates.
        for bad in ("", "x" * 121, "tool\ninjected", "tool\0injected", "tool\rinjected"):
            with self.subTest(toolchain=repr(bad)):
                with self.assertRaises(runtime.AgentRuntimeError):
                    runtime.resolve_profile({"preset": "agent-linux", "toolchain": [bad, "ok-tool"]})
                with self.assertRaises(runtime.AgentRuntimeError):
                    runtime.canonical_manifest("linux", "x64", "main", (bad,))
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux", "toolchain": ["go-1.23", "go-1.23"]})
        good = runtime.resolve_profile({"preset": "agent-linux", "toolchain": ["go-1.23"]})
        self.assertEqual(good.toolchain, ("go-1.23",))

    def test_detector_ignores_trailing_comment_installer_url(self):
        # A code line with a trailing comment containing an installer URL
        # is not a bootstrap install.
        body = (
            "run: |\n"
            "  run: echo ok # see https://opencode.ai/install\n"
        )
        self.assertFalse(runtime.normal_execution_uses_bootstrap_install(body))
        # And a trailing-comment probe does not suppress a real installer.
        installer_with_comment_probe = (
            "run: |\n"
            "  curl -fsSL --retry 3 https://opencode.ai/install | bash # command -v opencode\n"
        )
        self.assertTrue(runtime.normal_execution_uses_bootstrap_install(installer_with_comment_probe))

    def test_persistent_attachment_is_recorded_as_persistent_egress(self):
        # The no-persistent-egress qualification is only meaningful because
        # the provider can record persistent attachments. A deliberately
        # persistent network must populate the list (and fail the check).
        provider = runtime.FakeProvider()
        network = provider.create_network("digest", "job-x", now=1000.0, persistent=True)
        self.assertTrue(network.persistent)
        self.assertEqual(provider.persistent_egress_objects, [network.id])

    def test_detector_ignores_comment_only_probe_markers(self):
        # A comment mentioning the probe ahead of an unconditional
        # installer must not suppress bootstrap detection.
        body = (
            "# prepared agent runtime probe would go here\n"
            "run: |\n"
            "  curl -fsSL --retry 3 https://opencode.ai/install | bash\n"
        )
        self.assertTrue(runtime.normal_execution_uses_bootstrap_install(body))
        # Comment-only installer URLs do not count as bootstrap installs.
        comment_only = (
            "# see https://opencode.ai/install for docs\n"
            "run: |\n"
            "  echo hello\n"
        )
        self.assertFalse(runtime.normal_execution_uses_bootstrap_install(comment_only))

    def test_detector_rejects_bare_marker_code_before_installer(self):
        # A bare `prepared-runtime` marker on a code line (job name, echo
        # string) is not an executable probe: an unconditional installer
        # preceded only by such a string must still count as a bootstrap
        # install instead of being misclassified as a warm path.
        body = (
            "run: |\n"
            '  echo "prepared-runtime ready"\n'
            "  curl -fsSL --retry 3 https://opencode.ai/install | bash\n"
        )
        self.assertTrue(runtime.normal_execution_uses_bootstrap_install(body))
        # A real executable probe ahead of the installer is still the warm
        # path, even with marker prose nearby.
        warm = (
            "run: |\n"
            "  # prepared agent runtime lives on the golden image\n"
            "  command -v opencode >/dev/null 2>&1\n"
            '  echo "prepared-runtime hit"\n'
            "  exit 0\n"
            "  curl -fsSL --retry 3 https://opencode.ai/install | bash\n"
        )
        self.assertFalse(runtime.normal_execution_uses_bootstrap_install(warm))

    def test_resolve_profile_rejects_non_numeric_limits_as_domain_error(self):
        for declaration in (
            {"preset": "agent-linux", "concurrency_limit": "many"},
            {"preset": "agent-linux", "provisioning_timeout_seconds": "soon"},
            {"preset": "agent-linux", "max_job_lifetime_seconds": "long"},
        ):
            with self.subTest(declaration=declaration):
                with self.assertRaises(runtime.AgentRuntimeError):
                    runtime.resolve_profile(declaration)

    def test_provisioning_failure_requeues_demand_and_cleans_partial(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        controller.queue_job("acme/app", profile)
        original_create = controller.provider.create_instance

        def _boom(**kwargs):
            raise RuntimeError("provider outage")

        controller.provider.create_instance = _boom  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                controller.run_next_job(manifest, now=1000.0)
        finally:
            controller.provider.create_instance = original_create  # type: ignore[method-assign]
        self.assertEqual(len(controller.queued), 1)
        self.assertEqual(controller.live_idle_count(), 1 - 1)  # network cleaned, nothing live-idle leaked
        self.assertEqual(controller.live_instance_count(), 0)

    def test_restart_deep_copies_leases(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        controller.queue_job("acme/app", profile, run_id="r1")
        controller.run_next_job(manifest, now=1000.0)
        controller.queue_job("acme/app", profile, run_id="r2")
        restarted = controller.restart()
        restarted.leases["ghost"] = runtime.Lease(
            project_id="proj-a", repository="acme/app", job_id="ghost", run_id="g",
            profile_digest=runtime.profile_digest(profile), instance_id="ghost",
            created_at=0.0, lease_expires_at=1.0, max_age_at=2.0)
        self.assertNotIn("ghost", controller.leases)

    def test_multi_profile_active_pointers_are_independent(self):
        controller = _controller()
        linux = runtime.resolve_profile({"preset": "agent-linux"})
        macos = runtime.resolve_profile({"preset": "agent-macos"})
        linux_manifest = _manifest(linux)
        macos_manifest = _manifest(macos)
        controller.queue_job("acme/app", linux, run_id="r-linux")
        first = controller.run_next_job(linux_manifest, now=1000.0)
        self.assertTrue(first.destroyed)
        # Promoting the second platform must not invalidate the first:
        # each profile keeps its own active digest.
        controller.queue_job("acme/app", macos, run_id="r-macos")
        second = controller.run_next_job(macos_manifest, now=2000.0)
        self.assertTrue(second.destroyed)
        self.assertEqual(controller.images.active_digest_for(linux),
                         runtime.image_digest(linux_manifest, linux))
        self.assertEqual(controller.images.active_digest_for(macos),
                         runtime.image_digest(macos_manifest, macos))
        # A stale digest for one profile is still rejected for that
        # profile even while the other profile serves.
        stale_manifest = runtime.AgentManifest(
            **{**linux_manifest.to_canonical(), "opencode_version": "9.9.9"})
        controller.queue_job("acme/app", linux, run_id="r-stale")
        with self.assertRaises(runtime.AgentRuntimeError):
            controller.run_next_job(stale_manifest, now=3000.0)

    def test_top_level_network_lifecycle_persistent_is_rejected(self):
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux",
                                     "network_lifecycle": "persistent"})
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux",
                                     "network": "persistent"})

    def test_runner_probe_requires_executable_version_check(self):
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = runtime.canonical_manifest("linux", "x64", "main")
        weak = runtime.AgentManifest(**{**manifest.to_canonical(), "probes": (
            "opencode --version",
            "pr-agent --version",
            "echo runner",
        )})
        store = runtime.ImageStore(project_id="proj-probe")
        with self.assertRaises(runtime.AgentRuntimeError):
            store.ensure_image(weak, profile, now=1000.0)
        strong = runtime.AgentManifest(**{**manifest.to_canonical(), "probes": (
            "opencode --version",
            "pr-agent --version",
            "runner --version",
        )})
        strong_generation = store.ensure_image(strong, profile, now=2000.0)
        runtime.validate_image(strong, profile, strong_generation)

    def test_synthesized_metadata_without_probe_execution_does_not_validate(self):
        # SBOM/provenance version strings are synthesized from manifest
        # versions, so they prove nothing on their own: a hand-built
        # generation echoing the right versions but carrying no executed
        # probe record (a base image missing the binaries) must fail.
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = runtime.canonical_manifest("linux", "x64", "main")
        forged = runtime.ImageGeneration(
            digest=runtime.image_digest(manifest, profile),
            manifest=manifest,
            profile=profile,
            built_at=1000.0,
            validated=False,
            sbom=(
                "opencode=={}".format(manifest.opencode_version),
                "pr-agent=={}".format(manifest.pr_agent_version),
                "actions-runner=={}".format(manifest.runner_version),
            ),
            provenance="continuum-ref={} base={} probes=opencode=={},pr-agent=={},actions-runner=={}".format(
                manifest.continuum_ref,
                manifest.base_image,
                manifest.opencode_version,
                manifest.pr_agent_version,
                manifest.runner_version,
            ),
        )
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.validate_image(manifest, profile, forged)
        # The trusted build path records execution: ensure_image runs the
        # declared probes and the result validates.
        genuine = runtime.ImageStore(project_id="proj-probe-exec").ensure_image(
            manifest, profile, now=1000.0)
        self.assertEqual(tuple(genuine.executed_probes), tuple(manifest.probes))
        runtime.validate_image(manifest, profile, genuine)
        # A non-executable probe can never produce an execution record.
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.execute_manifest_probes(
                runtime.AgentManifest(**{**manifest.to_canonical(), "probes": ("echo runner",)}))

    def test_opencode_version_probe_is_equivalent_to_command_v(self):
        # `opencode --version` (the canonical manifest probe) plus pinned
        # version and digest counts as a prepared-runtime probe, exactly
        # like `command -v opencode`.
        workflow = (
            "run: |\n"
            "  if [[ \"${CONTINUUM_IMAGE_DIGEST:-}\" =~ ^[0-9a-f]{64}$ ]] && "
            "opencode --version 2>&1 | grep -E -q \"1.18.34\"; then\n"
            "    echo prepared-runtime hit 1.18.34\n"
            "  fi\n"
        )
        self.assertTrue(runtime.workflow_step_has_prepared_runtime_probe(workflow))

    def test_guarded_cache_miss_reconstruction_is_not_a_bootstrap_install(self):
        # A correctly guarded cache-miss branch after a probe only
        # reconstructs on a validated miss; it must not count as a
        # bootstrap install.
        guarded = (
            "run: |\n"
            "  command -v opencode >/dev/null 2>&1\n"
            "  opencode --version 2>&1 | grep -E -q \"1.18.34\"\n"
            "  if [ \"$CACHE_HIT\" != \"true\" ]; then curl -fsSL https://opencode.ai/install | bash; fi\n"
        )
        self.assertFalse(runtime.normal_execution_uses_bootstrap_install(guarded))
        # An unconditional installer after a bare probe still counts.
        bare = (
            "run: |\n"
            "  command -v opencode >/dev/null 2>&1\n"
            "  curl -fsSL https://opencode.ai/install | bash\n"
        )
        self.assertTrue(runtime.normal_execution_uses_bootstrap_install(bare))

    def test_live_idle_count_without_clock_exposes_expired_lease(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        manifest = _manifest(profile)
        digest = runtime.image_digest(manifest, profile)
        network = controller.provider.create_network(
            runtime.profile_digest(profile), "job-leak", now=1000.0)
        leaked = controller.provider.create_instance(
            project_id="proj-a", repository="acme/app",
            profile_digest=runtime.profile_digest(profile), digest=digest,
            job_id="job-leak", network_id=network.id, now=1000.0)
        controller.leases[leaked.id] = runtime.Lease(
            project_id="proj-a", repository="acme/app", job_id="job-leak",
            run_id="r-leak",
            profile_digest=runtime.profile_digest(profile),
            instance_id=leaked.id,
            created_at=1000.0,
            lease_expires_at=1000.0 + controller.max_job_lifetime,
            max_age_at=1000.0 + controller.global_max_age, state="running")
        # The lease expired long ago and the sweeper has not run: even
        # without an explicit clock the leaked live compute must count as
        # idle instead of reporting a vacuous zero.
        self.assertGreaterEqual(
            controller.live_idle_count(now=1000.0 + controller.global_max_age + 1.0), 1)
        self.assertGreaterEqual(controller.live_idle_count(), 1)

    def test_resolve_profile_from_env_reads_preset_and_provider(self):
        profile = runtime.resolve_profile_from_env(
            {"CONTINUUM_RUNTIME_PRESET": "agent-macos",
             "CONTINUUM_RUNTIME_PROVIDER": "gce"})
        self.assertEqual(profile.os, "macos")
        self.assertEqual(profile.provider, "gce")
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile_from_env(
                {"CONTINUUM_RUNTIME_PRESET": "no-such-preset"})
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile_from_env(
                {"CONTINUUM_RUNTIME_PRESET": "agent-linux",
                 "CONTINUUM_RUNTIME_PROVIDER": "no-such-provider"})

    def test_toolchain_string_is_rejected_like_features(self):
        # A bare string toolchain must not iterate into characters:
        # "nodejs" became ('n','o','d','e','j','s') and "gcc" tripped a
        # duplicate-character error instead of a type rejection.
        for bad in ("nodejs", "gcc", "", "go-1.23"):
            with self.subTest(toolchain=repr(bad)):
                with self.assertRaises(runtime.AgentRuntimeError):
                    runtime.resolve_profile({"preset": "agent-linux", "toolchain": bad})
                with self.assertRaises(runtime.AgentRuntimeError):
                    runtime.canonical_manifest("linux", "x64", "main", bad)
        for bad in (123, {"go-1.23"}, {"items": ["go-1.23"]}):
            with self.subTest(toolchain=repr(bad)):
                with self.assertRaises(runtime.AgentRuntimeError):
                    runtime.resolve_profile({"preset": "agent-linux", "toolchain": bad})
        # List and tuple inputs (including empty) still resolve.
        self.assertEqual(runtime.resolve_profile({"preset": "agent-linux"}).toolchain, ())
        self.assertEqual(
            runtime.resolve_profile(
                {"preset": "agent-linux", "toolchain": ["go-1.23"]}).toolchain,
            ("go-1.23",))
        self.assertEqual(
            runtime.resolve_profile(
                {"preset": "agent-linux", "toolchain": ("go-1.23",)}).toolchain,
            ("go-1.23",))

    def test_scalar_cache_false_disables_both_caches(self):
        # An explicit scalar disable must take effect instead of silently
        # leaving both caches enabled via the True defaults.
        disabled = runtime.resolve_profile({"preset": "agent-linux", "cache": False})
        self.assertFalse(disabled.golden_image)
        self.assertFalse(disabled.dependencies_cache)
        enabled = runtime.resolve_profile({"preset": "agent-linux", "cache": True})
        self.assertTrue(enabled.golden_image)
        self.assertTrue(enabled.dependencies_cache)
        # Mapping and top-level declarations keep working: the nested block
        # defers to an explicit top-level key instead of masking it.
        nested = runtime.resolve_profile(
            {"preset": "agent-linux", "cache": {"golden_image": False}})
        self.assertFalse(nested.golden_image)
        self.assertTrue(nested.dependencies_cache)
        top_level = runtime.resolve_profile(
            {"preset": "agent-linux", "golden_image": False})
        self.assertFalse(top_level.golden_image)
        self.assertTrue(top_level.dependencies_cache)
        mixed = runtime.resolve_profile(
            {"preset": "agent-linux", "cache": False, "dependencies": True})
        self.assertFalse(mixed.golden_image)
        self.assertTrue(mixed.dependencies_cache)
        with self.assertRaises(runtime.AgentRuntimeError):
            runtime.resolve_profile({"preset": "agent-linux", "cache": "disabled"})

    def test_profile_timeouts_govern_lease_expiry(self):
        # Consumer timeout tuning on the profile must reach the lease:
        # concurrency is already enforced from the profile while the lease
        # used only controller defaults.
        controller = _controller()
        profile = runtime.resolve_profile({
            "preset": "agent-linux",
            "provisioning_timeout_seconds": 60,
            "max_job_lifetime_seconds": 120,
        })
        manifest = _manifest(profile)
        self.assertNotEqual(profile.max_job_lifetime_seconds,
                            controller.max_job_lifetime)
        captured = {}
        real_lease = runtime.Lease

        def spy(**kwargs):
            captured.update(kwargs)
            return real_lease(**kwargs)

        controller.queue_job("acme/app", profile)
        with mock.patch("continuum.agent_runtime.Lease", side_effect=spy):
            result = controller.run_next_job(manifest, now=1000.0)
        self.assertEqual(result.outcome, "success")
        self.assertEqual(captured["lease_expires_at"], 1000.0 + 120)
        self.assertEqual(captured["max_age_at"],
                         1000.0 + max(controller.global_max_age, 120))
        self.assertEqual(captured["provisioning_timeout_seconds"], 60)

    def test_orphan_sweep_honors_per_lease_provisioning_timeout(self):
        controller = _controller()
        profile = runtime.resolve_profile({"preset": "agent-linux"})
        digest = runtime.image_digest(_manifest(profile), profile)
        network = controller.provider.create_network(
            runtime.profile_digest(profile), "job-slow", now=1000.0)
        slow = controller.provider.create_instance(
            project_id="proj-a", repository="acme/app",
            profile_digest=runtime.profile_digest(profile), digest=digest,
            job_id="job-slow", network_id=network.id, now=1000.0)
        # The profile allows a long provisioning window: past the
        # controller default (600s) the lease must not count as expired.
        controller.leases[slow.id] = runtime.Lease(
            project_id="proj-a", repository="acme/app", job_id="job-slow",
            run_id="r-slow", profile_digest=runtime.profile_digest(profile),
            instance_id=slow.id, created_at=1000.0,
            lease_expires_at=1000.0 + controller.max_job_lifetime,
            max_age_at=1000.0 + controller.global_max_age,
            state="provisioning", provisioning_timeout_seconds=3600.0)
        self.assertEqual(controller.live_idle_count(now=1000.0 + 601.0), 0)
        self.assertNotIn(slow.id, controller.sweep_orphans(now=1000.0 + 601.0))
        self.assertIn(slow.id, controller.sweep_orphans(now=1000.0 + 3601.0))

    def test_global_active_digest_is_none_when_profiles_diverge(self):
        # Sharing one store across platforms must not flip a global pointer
        # onto the other platform: per-profile pointers stay authoritative
        # and the global one refuses to pick a side.
        store = runtime.ImageStore(project_id="proj-multi")
        linux = runtime.resolve_profile({"preset": "agent-linux"})
        macos = runtime.resolve_profile({"preset": "agent-macos"})
        linux_gen = store.ensure_image(_manifest(linux), linux, now=1000.0)
        macos_gen = store.ensure_image(_manifest(macos), macos, now=2000.0)
        self.assertIsNone(store.active_digest)
        store.promote(linux_gen.digest, now=3000.0)
        self.assertEqual(store.active_digest, linux_gen.digest)
        store.promote(macos_gen.digest, now=4000.0)
        self.assertIsNone(store.active_digest)
        self.assertEqual(store.active_digest_for(linux), linux_gen.digest)
        self.assertEqual(store.active_digest_for(macos), macos_gen.digest)


if __name__ == "__main__":
    unittest.main()
