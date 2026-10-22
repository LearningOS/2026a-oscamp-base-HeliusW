"""Docker integration tests for source freshness; uses disposable Rust projects."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid


SPEC = importlib.util.spec_from_file_location(
    "oscamp", Path(__file__).resolve().parents[1] / "oscamp.py")
oscamp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oscamp)


class SourceFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oscamp-sync-test-")
        self.root = Path(self.temporary.name)
        self.remote = "/opt/oscamp-live/probe-" + uuid.uuid4().hex
        self.session = oscamp.SourceSync(
            self.root, workspace=self.remote + "/workspace", target=self.remote + "/target")
        (self.root / "Cargo.toml").write_text(
            '[workspace]\n[package]\nname="oscamp_freshness_test"\n'
            'version="0.0.0"\nedition="2021"\n')
        (self.root / "src").mkdir()
        self.source = self.root / "src/main.rs"
        self.write_version(1)

    def tearDown(self):
        self.session.close()
        subprocess.run(
            ["docker", "exec", oscamp.CONTAINER, "python3", "-c",
             "import shutil,sys; p=sys.argv[1]; "
             "assert p.startswith('/opt/oscamp-live/probe-'); shutil.rmtree(p, ignore_errors=True)",
             self.remote], check=True, timeout=15)
        self.temporary.cleanup()

    def write_version(self, version):
        self.source.write_text('fn main() { println!("version=' + str(version) + '"); }\n')

    def run_program(self):
        return subprocess.run(
            self.session.command(["cargo", "run", "--offline", "--quiet"]),
            capture_output=True, text=True, timeout=30)

    def assert_version(self, version):
        result = self.run_program()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "version=" + str(version))

    def test_saves_with_preserved_timestamp_and_atomic_replace(self):
        self.session.sync()
        self.assert_version(1)
        original = self.source.stat()
        self.write_version(2222222222222222)
        os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.session.sync()
        self.assert_version(2222222222222222)
        replacement = self.source.with_suffix(".tmp")
        replacement.write_text('fn main() { println!("version=3"); }\n')
        replacement.replace(self.source)
        self.session.sync()
        self.assert_version(3)
        # A broken save must report a build error, rather than run the old binary.
        self.source.write_text("fn main( { this is not valid Rust\n")
        self.session.sync()
        self.assertNotEqual(self.run_program().returncode, 0)
        self.write_version(4)
        self.session.sync()
        self.assert_version(4)

    def test_deleted_sources_are_removed(self):
        extra = self.root / "src/old_module.rs"
        extra.write_text("pub const OLD: u32 = 1;\n")
        self.session.sync()
        extra.unlink()
        report = self.session.sync()
        self.assertEqual(report["deleted"], 1)
        self.assertNotIn("src/old_module.rs", report["manifest"])

    def test_saved_change_is_automatic_and_emits_linux_watch_event(self):
        self.session.sync()
        # Watch Linux's inotify, the mechanism used by the course CLI.
        observer = r'''
import ctypes, os, select, sys
libc = ctypes.CDLL(None)
fd = libc.inotify_init1(0)
assert fd >= 0
wd = libc.inotify_add_watch(fd, sys.argv[1].encode(), 0x80)
assert wd >= 0
print("READY", flush=True)
assert select.select([fd], [], [], 15)[0], "No Linux file event"
event = os.read(fd, 65536)
assert b"main.rs" in event
print("CHANGED", flush=True)
'''
        process = subprocess.Popen(
            ["docker", "exec", oscamp.CONTAINER, "python3", "-u", "-c", observer,
             self.session.workspace + "/src"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            self.session.start()
            self.write_version(55)
            output, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, error)
            self.assertIn("CHANGED", output)
            deadline = time.monotonic() + 10
            expected = oscamp.signature(oscamp.saved_sources(self.root))
            while self.session.applied != expected and time.monotonic() < deadline:
                self.assertIsNone(self.session.error)
                time.sleep(0.1)
            self.assertEqual(self.session.applied, expected)
            self.assert_version(55)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
