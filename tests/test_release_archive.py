"""The properties a published archive is trusted for.

An archive is not a build intermediate. Its bytes are what a consumer downloads,
what a checksum file names, and what a re-run of the same revision has to
reproduce exactly — so these tests are about bytes, not about the writer's
internals: read the archive back with `tarfile` and `zipfile`, the way a consumer
would, and assert on what it finds.
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import tarfile
import tempfile
import unittest
import zipfile
from typing import Dict, Tuple

from continuum.release import archive as archive_module


class ArchiveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="continuum-archive-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.dist = os.path.join(self.root, "dist")
        os.makedirs(self.dist)

    def write(self, relative: str, contents: str = "x", *, executable: bool = False) -> str:
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(path, 0o755 if executable else 0o644)
        return path

    def bundle(self) -> str:
        """A shape like a real app bundle: a binary, a plist, and a link."""

        self.write("Demo.app/Contents/Info.plist", "CFBundleIdentifier = com.example\n")
        self.write("Demo.app/Contents/MacOS/demo", "MACH-O\n", executable=True)
        self.write("Demo.app/Contents/Frameworks/Real.framework/Versions/A/demo", "MACH-O\n")
        os.symlink(
            "A", os.path.join(self.root, "Demo.app/Contents/Frameworks/Real.framework/Current")
        )
        os.symlink(
            os.path.join("Versions", "Current", "demo"),
            os.path.join(self.root, "Demo.app/Contents/Frameworks/Real.framework/demo"),
        )
        return os.path.join(self.root, "Demo.app")

    def members(self, root: str, name: str = "") -> Tuple[archive_module.Member, ...]:
        """The members of an archive of everything under `root`.

        `name` is the prefix the members take inside the archive; empty means the
        root itself, which is how the adapter lists a bundle ("Demo.app") without
        repeating it at every call site.
        """

        if name:
            return archive_module.members_for(root, (name,))
        return archive_module.members_for(root, tuple(sorted(os.listdir(root))))

    def tar_entries(self, path: str) -> Dict[str, tarfile.TarInfo]:
        with tarfile.open(path, "r:gz") as archive:
            return {item.name: item for item in archive.getmembers()}

    def zip_entries(self, path: str) -> Dict[str, zipfile.ZipInfo]:
        with zipfile.ZipFile(path) as archive:
            return {item.filename: item for item in archive.infolist()}

    def digest(self, path: str) -> str:
        from continuum.release.contract import digest_file

        return digest_file(path, "sha256")[1]


class LayoutTests(ArchiveTestCase):
    def test_lists_every_file_under_the_root_sorted_by_name(self):
        self.bundle()
        names = [member.name for member in self.members(self.root, "Demo.app")]
        self.assertEqual(names, sorted(names))
        self.assertIn("Demo.app/Contents/Info.plist", names)
        self.assertIn("Demo.app/Contents/MacOS/demo", names)
        self.assertIn("Demo.app/Contents/Frameworks/Real.framework/Versions/A/demo", names)

    def test_keeps_the_tree_shape_rather_than_flattening_it(self):
        """A bundle's value is in its paths.

        `Contents/Frameworks/X.framework/demo` unpacked as `demo` is not the
        framework the bundle referenced, and macOS refuses to load the result.
        """

        self.bundle()
        names = [member.name for member in self.members(self.root, "Demo.app")]
        self.assertIn("Demo.app/Contents/Frameworks/Real.framework/demo", names)
        self.assertIn("Demo.app/Contents/Frameworks/Real.framework/Current", names)

    def test_names_members_relative_to_the_root_they_were_listed_from(self):
        self.write("nested/deep/file.txt", "x")
        root = os.path.join(self.root, "nested")
        self.assertEqual([m.name for m in self.members(root)], ["deep/file.txt"])

    def test_records_a_link_to_a_directory_as_a_member_of_its_own(self):
        """A framework's `Versions/Current` is a link, and `os.walk` will not
        descend into it.

        Treating it as a directory entry produces an archive with either a
        missing link or a duplicated tree, depending on the walker's follow-symlinks
        setting — so the two cases have to be told apart rather than discovered.
        """

        self.write("Tree/real/file.txt", "x")
        os.symlink("real", os.path.join(self.root, "Tree/link"))
        members = {m.name: m for m in self.members(self.root, "Tree")}
        self.assertEqual(sorted(members), ["Tree/link", "Tree/real/file.txt"])
        self.assertTrue(os.path.islink(members["Tree/link"].source))

    def test_keeps_only_the_members_named_when_a_prefix_is_given(self):
        self.bundle()
        names = [
            member.name
            for member in archive_module.members_for(
                os.path.join(self.root, "Demo.app"), ("Contents",)
            )
        ]
        self.assertTrue(names)
        self.assertTrue(all(name.startswith("Contents/") for name in names))
        self.assertNotIn("Package.swift", " ".join(names))

    def test_refuses_a_member_name_that_would_write_outside_the_archive(self):
        for name in ("/etc/passwd", "../escape", "a/../../b"):
            with self.assertRaises(archive_module.ArchiveError):
                archive_module.Member(source=self.write("real.txt", "x"), name=name)

    def test_refuses_two_groups_that_claim_one_name(self):
        left = self.write("left/thing", "one")
        right = self.write("right/thing", "two")
        with self.assertRaises(archive_module.ArchiveError) as caught:
            archive_module.combine(
                (archive_module.Member(source=left, name="thing"),),
                (archive_module.Member(source=right, name="thing"),),
            )
        self.assertEqual(caught.exception.code, "archive-member-conflict")

    def test_tolerates_two_groups_that_claim_a_name_with_the_same_source(self):
        source = self.write("thing", "one")
        member = archive_module.Member(source=source, name="thing")
        self.assertEqual(archive_module.combine((member,), (member,)), (member,))


class TarGzTests(ArchiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root_dir = self.bundle()
        self.path = os.path.join(self.dist, "demo.tar.gz")
        archive_module.write(
            self.path, self.members(self.root, "Demo.app"), artifact_format="tar.gz"
        )

    def test_carries_the_same_files_the_walker_listed(self):
        self.assertEqual(
            sorted(self.tar_entries(self.path)),
            sorted(member.name for member in self.members(self.root, "Demo.app")),
        )

    def test_records_every_member_at_the_fixed_epoch(self):
        for name, entry in self.tar_entries(self.path).items():
            self.assertEqual(entry.mtime, archive_module.DEFAULT_EPOCH, name)

    def test_honours_an_explicit_epoch(self):
        path = os.path.join(self.dist, "epoch.tar.gz")
        archive_module.write(
            path, self.members(self.root, "Demo.app"), artifact_format="tar.gz", epoch=1700000000
        )
        for entry in self.tar_entries(path).values():
            self.assertEqual(entry.mtime, 1700000000)

    def test_takes_the_epoch_from_the_job_when_it_sets_one(self):
        self.assertEqual(
            archive_module.epoch_from({"SOURCE_DATE_EPOCH": "1700000000"}), 1700000000
        )
        self.assertEqual(archive_module.epoch_from({}), archive_module.DEFAULT_EPOCH)

    def test_records_no_owner_and_no_owner_name(self):
        for entry in self.tar_entries(self.path).values():
            self.assertEqual((entry.uid, entry.gid), (0, 0))
            self.assertEqual((entry.uname, entry.gname), ("", ""))

    def test_records_the_executable_bit_and_nothing_else(self):
        entries = self.tar_entries(self.path)
        self.assertEqual(entries["Demo.app/Contents/MacOS/demo"].mode, 0o755)
        self.assertEqual(entries["Demo.app/Contents/Info.plist"].mode, 0o644)

    def test_keeps_a_link_a_link(self):
        entry = self.tar_entries(self.path)["Demo.app/Contents/Frameworks/Real.framework/Current"]
        self.assertTrue(entry.issym())
        self.assertEqual(entry.linkname, "A")
        self.assertEqual(entry.size, 0, "a link carries no body")

    def test_the_gzip_header_names_no_file_and_no_time(self):
        """`gzip -n` behaviour, without the tool.

        A file name in the header makes two archives of identical contents differ
        when they are written from different directories.
        """

        with open(self.path, "rb") as handle:
            header = handle.read(10)
        self.assertEqual(header[:2], b"\x1f\x8b")
        self.assertEqual(header[3], 0, "the gzip flags byte must be empty")
        self.assertEqual(int.from_bytes(header[4:8], "little"), 0, "mtime must be zero")
        self.assertEqual(
            header[3], 0, "the gzip flags byte must be empty: no name, no comment"
        )

    def test_two_writes_of_the_same_tree_are_byte_identical(self):
        again = os.path.join(self.dist, "again.tar.gz")
        archive_module.write(again, self.members(self.root, "Demo.app"), artifact_format="tar.gz")
        self.assertEqual(self.digest(self.path), self.digest(again))

    def test_a_umask_does_not_reach_the_archive(self):
        """Only the executable bit is a fact about the artifact.

        A checkout that preserved a group-write bit, or a build under a
        different umask, must not produce different bytes for the same release.
        """

        binary = os.path.join(self.root_dir, "Contents", "MacOS", "demo")
        first = os.path.join(self.dist, "0755.tar.gz")
        archive_module.write(first, self.members(self.root, "Demo.app"), artifact_format="tar.gz")
        for noisy, second in ((0o775, "0775"), (0o700, "0700"), (0o755, "0755")):
            os.chmod(binary, noisy)
            path = os.path.join(self.dist, f"{second}.tar.gz")
            archive_module.write(path, self.members(self.root, "Demo.app"), artifact_format="tar.gz")
            self.assertEqual(
                self.tar_entries(path)["Demo.app/Contents/MacOS/demo"].mode,
                0o755,
                f"mode {oct(noisy)} leaked into the archive",
            )
            self.assertEqual(
                self.digest(path), self.digest(first), f"{oct(noisy)} changed the bytes"
            )
        os.chmod(binary, 0o644)
        data = os.path.join(self.dist, "0644.tar.gz")
        archive_module.write(data, self.members(self.root, "Demo.app"), artifact_format="tar.gz")
        self.assertEqual(
            self.tar_entries(data)["Demo.app/Contents/MacOS/demo"].mode, 0o644
        )


class ZipTests(ArchiveTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root_dir = self.bundle()
        self.path = os.path.join(self.dist, "demo.zip")
        archive_module.write(self.path, self.members(self.root, "Demo.app"), artifact_format="zip")

    def test_carries_the_same_files_the_walker_listed(self):
        self.assertEqual(
            sorted(self.zip_entries(self.path)),
            sorted(member.name for member in self.members(self.root, "Demo.app")),
        )

    def test_records_every_entry_at_the_same_fixed_date(self):
        for name, entry in self.zip_entries(self.path).items():
            self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0), name)

    def test_keeps_a_link_a_link(self):
        """A zip that unpacks a framework into files is not a framework.

        The type bits in the entry's mode are the only thing that says "this is
        a link"; without them the archive installs a tree of ordinary files and
        macOS refuses to load it, with nothing in the archive to explain why.
        """

        entry = self.zip_entries(self.path)["Demo.app/Contents/Frameworks/Real.framework/Current"]
        mode = entry.external_attr >> 16
        self.assertEqual(stat.S_IFMT(mode), stat.S_IFLNK, oct(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o777)
        with zipfile.ZipFile(self.path) as archive:
            self.assertEqual(archive.read(entry), b"A")

    def test_records_an_ordinary_file_as_an_ordinary_file(self):
        mode = self.zip_entries(self.path)["Demo.app/Contents/Info.plist"].external_attr >> 16
        self.assertEqual(stat.S_IFMT(mode), stat.S_IFREG, oct(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o644)

    def test_records_the_executable_bit(self):
        mode = self.zip_entries(self.path)["Demo.app/Contents/MacOS/demo"].external_attr >> 16
        self.assertEqual(stat.S_IMODE(mode), 0o755)

    def test_two_writes_of_the_same_tree_are_byte_identical(self):
        again = os.path.join(self.dist, "again.zip")
        archive_module.write(again, self.members(self.root, "Demo.app"), artifact_format="zip")
        self.assertEqual(self.digest(self.path), self.digest(again))

    def test_both_formats_of_one_tree_carry_one_release(self):
        """A target that declares both formats is promising the same thing twice.

        The property that makes that promise true is per-member identity: same
        names, same bytes, same link targets.
        """

        tar_path = os.path.join(self.dist, "demo.tar.gz")
        archive_module.write(
            tar_path, self.members(self.root, "Demo.app"), artifact_format="tar.gz"
        )
        tar_entries = self.tar_entries(tar_path)
        zip_entries = self.zip_entries(self.path)
        self.assertEqual(sorted(tar_entries), sorted(zip_entries))
        with tarfile.open(tar_path, "r:gz") as tar, zipfile.ZipFile(self.path) as zipped:
            for name, entry in tar_entries.items():
                if entry.issym():
                    self.assertEqual(zipped.read(name), entry.linkname.encode("utf-8"))
                    continue
                self.assertEqual(
                    tar.extractfile(entry).read(), zipped.read(name), name
                )


class FormatTests(ArchiveTestCase):
    def test_names_a_format_by_its_extension(self):
        self.assertEqual(archive_module.extension_for("tar.gz"), ".tar.gz")
        self.assertEqual(archive_module.extension_for("zip"), ".zip")

    def test_refuses_a_format_it_cannot_write(self):
        thing = self.write("thing", "x")
        for bad in ("7z", "rar", ""):
            with self.assertRaises(archive_module.ArchiveError) as caught:
                archive_module.extension_for(bad)
            self.assertEqual(caught.exception.code, "archive-format-unsupported")
            with self.assertRaises(archive_module.ArchiveError):
                archive_module.write(
                    os.path.join(self.dist, "out"),
                    (archive_module.Member(source=thing, name="thing"),),
                    artifact_format=bad,
                )

    def test_a_refusal_names_the_formats_it_does_support(self):
        with self.assertRaises(archive_module.ArchiveError) as caught:
            archive_module.extension_for("7z")
        self.assertIn("tar.gz", str(caught.exception))
        self.assertIn("zip", str(caught.exception))

    def test_reports_the_path_it_could_not_write(self):
        """A missing parent directory is created; anything else is reported.

        A writer that raised on a `dist` the job had not created yet would fail
        every first run, and one that swallowed a real I/O error would publish a
        truncated archive. The refusal has to name the path either way.
        """

        thing = self.write("thing", "x")
        member = (archive_module.Member(source=thing, name="thing"),)
        nested = os.path.join(self.dist, "made", "on", "demand.tar.gz")
        archive_module.write(nested, member, artifact_format="tar.gz")
        self.assertTrue(os.path.isfile(nested))

        blocker = os.path.join(self.root, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("a file where a directory would have to be")
        with self.assertRaises(archive_module.ArchiveError) as caught:
            archive_module.write(
                os.path.join(blocker, "out.tar.gz"), member, artifact_format="tar.gz"
            )
        self.assertIn(blocker, str(caught.exception))

    def test_names_the_format_it_was_asked_for_in_the_refusal(self):
        thing = self.write("thing", "x")
        with self.assertRaises(archive_module.ArchiveError) as caught:
            archive_module.write(
                os.path.join(self.dist, "out"),
                (archive_module.Member(source=thing, name="thing"),),
                artifact_format="7z",
            )
        self.assertIn("7z", str(caught.exception))


class ReaderTests(ArchiveTestCase):
    """The archives are read by tools, not by this repository.

    A gzip stream that this module's own `tarfile` call can open is not evidence
    that `tar -tzf` or `unzip -l` can.
    """

    def test_system_tar_reads_the_archive_and_sees_the_link(self):
        self.bundle()
        path = os.path.join(self.dist, "demo.tar.gz")
        archive_module.write(
            path, self.members(self.root, "Demo.app"),
            artifact_format="tar.gz",
        )
        import subprocess

        listing = subprocess.run(
            ["tar", "-tvzf", path], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("Demo.app/Contents/Info.plist", listing)
        self.assertIn("-> A", listing)

    def test_system_unzip_reads_the_archive_and_sees_the_link(self):
        self.bundle()
        path = os.path.join(self.dist, "demo.zip")
        archive_module.write(
            path, self.members(self.root, "Demo.app"),
            artifact_format="zip",
        )
        import subprocess

        listing = subprocess.run(
            ["unzip", "-Z", "-l", path], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("Demo.app/Contents/Frameworks/Real.framework/Current", listing)
        self.assertIn("l", listing.splitlines()[1][:10])

    def test_the_extracted_tree_matches_the_source(self):
        self.bundle()
        path = os.path.join(self.dist, "demo.tar.gz")
        archive_module.write(
            path, self.members(self.root, "Demo.app"),
            artifact_format="tar.gz",
        )
        target = os.path.join(self.root, "extracted")
        os.makedirs(target)
        import subprocess

        subprocess.run(["tar", "-xzf", path, "-C", target], check=True)
        link = os.path.join(
            target, "Demo.app/Contents/Frameworks/Real.framework/Current"
        )
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), "A")
        with open(os.path.join(target, "Demo.app/Contents/Info.plist")) as handle:
            self.assertIn("com.example", handle.read())


class MissingSourceTests(ArchiveTestCase):
    def test_refuses_to_write_an_archive_of_a_missing_source(self):
        with self.assertRaises(archive_module.ArchiveError) as caught:
            archive_module.write(
                os.path.join(self.dist, "out.tar.gz"),
                (archive_module.Member(source=os.path.join(self.root, "gone"), name="gone"),),
                artifact_format="tar.gz",
            )
        self.assertIn("gone", str(caught.exception))

    def test_a_declared_member_that_is_missing_stops_the_release(self):
        """`write` reports rather than writing a partial archive.

        An archive that silently omits a member publishes a bundle with a hole
        in it, and the checksum file records that hole as if it were the release.
        """

        present = self.write("thing", "contents")
        with self.assertRaises(archive_module.ArchiveError):
            archive_module.write(
                os.path.join(self.dist, "out.tar.gz"),
                (
                    archive_module.Member(source=present, name="thing"),
                    archive_module.Member(source=os.path.join(self.root, "gone"), name="gone"),
                ),
                artifact_format="tar.gz",
            )