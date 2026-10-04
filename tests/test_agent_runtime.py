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
        for name in ("continuum-opencode.yml", "continuum-pr-agent.yml",
                     "continuum-pr-agent-repair.yml"):
            path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", name)
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            self.assertFalse(runtime.normal_execution_uses_bootstrap_install(body),
                             "{}: warm path must probe the prepared runtime, not bootstrap-install".format(name))
            self.assertTrue(runtime.workflow_step_has_prepared_runtime_probe(body),
                            "{}: prepared-runtime probe with pinned versions + image digest required".format(name))

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
        self.assertEqual(store.active_digest, gen.digest)

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


if __name__ == "__main__":
    unittest.main()
