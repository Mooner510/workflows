"""Exercise the OCI artifact scan shell with disposable archives and a scanner double."""

import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import textwrap
import unittest


ACTION = Path(__file__).resolve().parents[1] / ".github/actions/ci/oci-artifact/action.yml"


class OCIScanTest(unittest.TestCase):
    def run_scan(self, *, complete=True, scanner_status=0, corrupt=False):
        source = ACTION.read_text()
        start = source.index('        scan_layout="$(mktemp')
        end = source.index('        archive_digest=', start)
        script = "set -euo pipefail\n" + textwrap.dedent(source[start:end])
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            archive = work / "image.oci.tar"
            if corrupt:
                archive.write_bytes(b"invalid tar")
            else:
                with tarfile.open(archive, "w") as bundle:
                    files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
                             "index.json": b'{"schemaVersion":2,"manifests":[]}'}
                    if not complete:
                        del files["index.json"]
                    for name, content in files.items():
                        entry = tarfile.TarInfo(name)
                        entry.size = len(content)
                        bundle.addfile(entry, io.BytesIO(content))
                    entry = tarfile.TarInfo("blobs")
                    entry.type = tarfile.DIRTYPE
                    bundle.addfile(entry)
            original = archive.read_bytes()
            scanner = work / "trivy"
            scanner.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import os
                from pathlib import Path
                import sys
                args = sys.argv[1:]
                assert args[0] == 'image'
                layout = Path(args[args.index('--input') + 1])
                assert layout.is_dir()
                assert layout.parent == Path(os.environ['work'])
                assert (layout / 'oci-layout').is_file()
                assert (layout / 'index.json').is_file()
                assert (layout / 'blobs').is_dir()
                assert args[args.index('--scanners') + 1] == 'vuln'
                assert args[args.index('--severity') + 1] == 'HIGH,CRITICAL'
                assert args[args.index('--exit-code') + 1] == '1'
                Path(os.environ['work'], 'scanner-called').touch()
                sys.exit(int(os.environ['SCANNER_STATUS']))
                """))
            scanner.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={**os.environ, "work": str(work), "archive": str(archive),
                     "trivy": str(scanner), "SCANNER_STATUS": str(scanner_status)},
            )
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(list(work.glob(".scan-*")), [])
            return result, (work / "scanner-called").exists()

    def test_valid_layout(self):
        result, called = self.run_scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(called)

    def test_vulnerability_gate_failure(self):
        result, called = self.run_scan(scanner_status=1)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(called)

    def test_incomplete_layout_fails_before_scan(self):
        result, called = self.run_scan(complete=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OCI scan layout is incomplete", result.stdout)
        self.assertFalse(called)

    def test_corrupt_archive_cleanup(self):
        result, called = self.run_scan(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
