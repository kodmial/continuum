"""Durable receipts: resume, conflict, generations, and corruption."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from continuum.release.contract import (
    ArtifactManifest,
    ManifestBuilder,
    TYPE_ARCHIVE,
    digest_file,
)
from continuum.release.receipt import (
    CompositeReceiptStore,
    FileReceiptStore,
    MemoryReceiptStore,
    Receipt,
    ReceiptError,
    TagReceiptStore,
    receipt_name,
)
from continuum.release.state import COMPLETED, SKIPPED, Journal, StageOutcome

SHA = "a" * 40
OTHER_SHA = "b" * 40
VERSION = "1.2.3"


def manifest(directory: str, *, sha: str = SHA, version: str = VERSION, target: str = "linux"):
    path = os.path.join(directory, f"{target}.tar.gz")
    with open(path, "wb") as handle:
        handle.write(b"payload for " + target.encode())
    builder = ManifestBuilder(target=target, adapter="fixture", source_sha=sha, version=version)
    builder.record("widget.tar.gz", path, TYPE_ARCHIVE)
    return builder.build()


def receipt(directory: str, *, sha: str = SHA, version: str = VERSION, published=False):
    return Receipt(
        repository="example/widgets",
        version=version,
        tag=f"v{version}",
        source_sha=sha,
        manifests=(manifest(directory, sha=sha, version=version),),
        published=published,
    )


class ReceiptValueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def make(self, **overrides):
        values = dict(
            repository="example/widgets",
            version=VERSION,
            tag="v1.2.3",
            source_sha=SHA,
            manifests=(manifest(self.directory.name),),
        )
        values.update(overrides)
        return Receipt(**values)

    def test_round_trip_preserves_identity_and_assets(self):
        original = self.make()
        restored = Receipt.from_json(original.to_json())
        self.assertEqual(restored, original)
        self.assertEqual(restored.asset_identities, original.asset_identities)

    def test_a_malformed_document_is_refused(self):
        with self.assertRaises(ReceiptError) as caught:
            Receipt.from_json("{not json")
        self.assertEqual(caught.exception.code, "malformed-receipt")

    def test_an_unknown_schema_is_refused(self):
        payload = self.make().describe()
        payload["schema"] = "continuum.release-receipt/v9"
        with self.assertRaises(ReceiptError) as caught:
            Receipt.from_describe(payload)
        self.assertEqual(caught.exception.code, "receipt-schema-unknown")

    def test_a_receipt_without_a_version_is_refused(self):
        with self.assertRaises(ReceiptError):
            self.make(version="")

    def test_a_different_source_for_one_version_is_a_conflict(self):
        first = self.make()
        second = self.make(source_sha=OTHER_SHA, manifests=(manifest(self.directory.name, sha=OTHER_SHA),))
        with self.assertRaises(ReceiptError) as caught:
            first.assert_compatible(second)
        self.assertEqual(caught.exception.code, "receipt-conflict")

    def test_two_manifests_for_one_target_are_refused(self):
        first = manifest(self.directory.name, target="linux")
        changed = os.path.join(self.directory.name, "linux-signed.tar.gz")
        with open(changed, "wb") as handle:
            handle.write(b"signed payload")
        builder = ManifestBuilder(
            target="linux", adapter="fixture", source_sha=SHA, version=VERSION
        )
        builder.record("widget.tar.gz", changed, TYPE_ARCHIVE)
        with self.assertRaises(ReceiptError) as caught:
            self.make(manifests=(first, builder.build()))
        self.assertEqual(caught.exception.code, "receipt-conflict")

    def test_a_published_asset_set_is_immutable(self):
        first = self.make(published=True)
        delayed = self.make(
            manifests=(manifest(self.directory.name, target="windows"),)
        )
        with self.assertRaises(ReceiptError) as caught:
            first.assert_compatible(delayed)
        self.assertEqual(caught.exception.code, "asset-set-conflict")

    def test_merge_keeps_both_journals_and_the_published_flag(self):
        first = self.make()
        second = self.make(published=True)
        merged = first.merge(second)
        self.assertTrue(merged.published)
        self.assertEqual(len(merged.manifests), 1)

    def test_a_later_noop_does_not_undo_an_earlier_completion(self):
        # A duplicate event re-runs the destination stage, finds the release
        # already published, and records a no-op. The stored receipt says the
        # draft completed. Both are true: the work was already done.
        stored = self.make(
            published=True,
            journal=Journal(
                entries=(StageOutcome.completed("draft", "draft-key", "drafted"),)
            ),
        )
        rebuilt = self.make(
            journal=Journal(
                entries=(
                    StageOutcome.noop(
                        "draft",
                        "draft-key",
                        "already published",
                        code="already-published",
                    ),
                )
            )
        )
        merged = stored.merge(rebuilt)
        entries = [item for item in merged.journal.entries if item.stage == "draft"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome, COMPLETED)

    def test_a_later_completion_replaces_an_earlier_noop(self):
        noop_first = self.make(
            journal=Journal(
                entries=(
                    StageOutcome.noop("draft", "draft-key", "nothing yet"),
                )
            )
        )
        completed = self.make(
            journal=Journal(
                entries=(StageOutcome.completed("draft", "draft-key", "drafted"),)
            )
        )
        merged = noop_first.merge(completed)
        entries = [item for item in merged.journal.entries if item.stage == "draft"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome, COMPLETED)

    def test_a_later_duplicate_skip_does_not_undo_an_earlier_completion(self):
        stored = self.make(
            published=True,
            journal=Journal(
                entries=(StageOutcome.completed("publish", "publish-key", "published"),)
            ),
        )
        rebuilt = self.make(
            journal=Journal(
                entries=(
                    StageOutcome.skipped(
                        "publish",
                        "publish-key",
                        "every destination already had it",
                        duplicate=True,
                        code="duplicate-event",
                    ),
                )
            )
        )
        merged = stored.merge(rebuilt)
        entries = [item for item in merged.journal.entries if item.stage == "publish"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome, COMPLETED)

    def test_a_later_failure_still_conflicts_with_a_completion(self):
        stored = self.make(
            journal=Journal(
                entries=(StageOutcome.completed("draft", "draft-key", "drafted"),)
            )
        )
        failed = self.make(
            journal=Journal(
                entries=(
                    StageOutcome.failed(
                        "draft", "draft-key", "could not draft", code="draft-not-created"
                    ),
                )
            )
        )
        with self.assertRaises(ReceiptError) as caught:
            stored.merge(failed)
        self.assertEqual(caught.exception.code, "receipt-conflict")

    def test_merge_reconciles_a_stored_receipt_with_a_rebuild_elsewhere(self):
        # A published receipt names the release asset; a duplicate run rebuilds
        # the same bytes into a different workspace path. They are one release.
        second_directory = tempfile.TemporaryDirectory()
        self.addCleanup(second_directory.cleanup)
        stored = self.make(published=True)
        rebuilt = Receipt(
            repository="example/widgets",
            version=VERSION,
            tag="v1.2.3",
            source_sha=SHA,
            manifests=(manifest(second_directory.name),),
        )
        merged = stored.merge(rebuilt)
        self.assertEqual(len(merged.manifests), 1)
        self.assertTrue(merged.published)

    def test_merge_still_refuses_different_bytes_built_elsewhere(self):
        second_directory = tempfile.TemporaryDirectory()
        self.addCleanup(second_directory.cleanup)
        changed = os.path.join(second_directory.name, "linux.tar.gz")
        with open(changed, "wb") as handle:
            handle.write(b"different payload")
        builder = ManifestBuilder(
            target="linux", adapter="fixture", source_sha=SHA, version=VERSION
        )
        builder.record("widget.tar.gz", changed, TYPE_ARCHIVE)
        with self.assertRaises(ReceiptError) as caught:
            self.make(published=True).merge(
                Receipt(
                    repository="example/widgets",
                    version=VERSION,
                    tag="v1.2.3",
                    source_sha=SHA,
                    manifests=(builder.build(),),
                )
            )
        self.assertEqual(caught.exception.code, "asset-set-conflict")

    def test_a_receipt_name_is_derived_from_repository_and_version(self):
        self.assertEqual(receipt_name("example/widgets", "1.2.3"), "receipt-example-widgets-1.2.3.json")
        with self.assertRaises(ReceiptError):
            receipt_name("no-slash", "1.2.3")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_a_file_store_round_trips(self):
        store = FileReceiptStore(self.directory.name)
        saved = store.save(receipt(self.directory.name))
        self.assertIsNotNone(saved)
        loaded = store.load("example/widgets", VERSION)
        self.assertEqual(loaded.source_sha, SHA)
        self.assertEqual(loaded.tag, "v1.2.3")

    def test_a_second_save_merges_rather_than_replaces(self):
        store = FileReceiptStore(self.directory.name)
        first = store.save(receipt(self.directory.name))
        second = store.save(
            Receipt(
                repository=first.repository,
                version=first.version,
                tag=first.tag,
                source_sha=first.source_sha,
                manifests=first.manifests,
                published=True,
            )
        )
        self.assertTrue(second.published)
        self.assertTrue(store.load("example/widgets", VERSION).published)

    def test_a_conflicting_save_is_refused(self):
        store = FileReceiptStore(self.directory.name)
        store.save(receipt(self.directory.name))
        with self.assertRaises(ReceiptError):
            store.save(
                Receipt(
                    repository="example/widgets",
                    version=VERSION,
                    tag="v1.2.3",
                    source_sha=OTHER_SHA,
                    manifests=(manifest(self.directory.name, sha=OTHER_SHA),),
                )
            )

    def test_a_corrupt_file_fails_closed(self):
        store = FileReceiptStore(self.directory.name)
        store.save(receipt(self.directory.name))
        path = os.path.join(self.directory.name, receipt_name("example/widgets", VERSION))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not a receipt")
        with self.assertRaises(ReceiptError):
            store.load("example/widgets", VERSION)

    def test_an_absent_receipt_is_none_not_an_error(self):
        self.assertIsNone(FileReceiptStore(self.directory.name).load("example/widgets", "9.9.9"))

    def test_a_composite_store_merges_every_copy(self):
        first = MemoryReceiptStore()
        second = MemoryReceiptStore()
        composite = CompositeReceiptStore((first, second))
        composite.save(receipt(self.directory.name))
        self.assertIsNotNone(first.load("example/widgets", VERSION))
        self.assertIsNotNone(second.load("example/widgets", VERSION))
        self.assertIsNotNone(composite.load("example/widgets", VERSION))

    def test_a_composite_store_needs_a_store(self):
        with self.assertRaises(ReceiptError):
            CompositeReceiptStore(())

    def test_a_tag_store_refuses_a_receipt_for_another_tag(self):
        memory = MemoryTagStore()
        store = TagReceiptStore(memory, tag="v1.2.3")
        store.save(receipt(self.directory.name))
        self.assertEqual(
            Receipt.from_json(memory.documents["v1.2.3"]).version, VERSION
        )
        with self.assertRaises(ReceiptError):
            store.save(
                Receipt(
                    repository="example/widgets",
                    version=VERSION,
                    tag="v9.9.9",
                    source_sha=SHA,
                    manifests=(manifest(self.directory.name),),
                )
            )

    def test_a_tag_store_refuses_a_document_for_another_release(self):
        memory = MemoryTagStore()
        memory.save("v1.2.3", receipt(self.directory.name, version="9.9.9").to_json())
        with self.assertRaises(ReceiptError):
            TagReceiptStore(memory, tag="v1.2.3").load("example/widgets", VERSION)


class MemoryTagStore:
    def __init__(self):
        self.documents = {}

    def load(self, tag):
        return self.documents.get(tag)

    def save(self, tag, document):
        self.documents[tag] = document
        return f"{tag}.json"


if __name__ == "__main__":
    unittest.main()
