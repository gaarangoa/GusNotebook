"""Official artifact selection, validation, atomic installation and discovery."""

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from gusnotebook.git_auth import executable as git_executable
from gusnotebook.git_process import GitError
from gusnotebook.tool_installer import ToolInstaller, target
from gusnotebook.tunnels import executable as tunnel_executable


def archive(payload, member, kind="zip", symlink=False):
    buffer = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buffer, 'w') as package:
            info = zipfile.ZipInfo(member)
            if symlink:
                info.external_attr = 0o120777 << 16
            package.writestr(info, payload)
            package.writestr('../../outside', b'never extracted')
    else:
        with tarfile.open(fileobj=buffer, mode='w:gz') as package:
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = '../../outside'
                info.size = 0
            package.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve() / 'tools'
        self.installer = ToolInstaller(self.directory)
        self.finder = patch.object(self.installer, 'find', side_effect=lambda name:
            str(self.directory / name) if (self.directory / name).exists() else None)
        self.finder.start()
        self.environment = patch.dict(os.environ, {'GUSNOTEBOOK_GH':'', 'GUSNOTEBOOK_DEVTUNNEL':''})
        self.environment.start()
        self.cancel = threading.Event()
        self.progress = []

    def tearDown(self):
        self.installer.close()
        self.finder.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def download(self, payload, member, *, system='macOS', arch='arm64', digest=None, symlink=False):
        asset = archive(payload, member, 'zip' if system == 'macOS' else 'tar', symlink=symlink)
        suffix = 'zip' if system == 'macOS' else 'tar.gz'
        name = f'gh_1.2.3_{system}_{arch}.{suffix}'
        metadata = json.dumps({'tag_name':'v1.2.3','assets':[{'name':name,
            'digest':'sha256:' + (digest or hashlib.sha256(asset).hexdigest())}]}).encode()
        def response(url, destination, cancel, progress, **kwargs):
            destination.write_bytes(metadata if 'api.github.com' in url else asset)
            progress(state='downloading', received=len(asset), total=len(asset))
        return patch.object(self.installer, '_download', side_effect=response)

    def test_mac_zip_and_linux_tar_publish_only_the_validated_executable(self):
        payload = b'#!/bin/sh\nprintf "gh version 1.2.3\\n"\n'
        for system, arch in [('macOS','arm64'), ('linux','amd64')]:
            member = f'gh_1.2.3_{system}_{arch}/bin/gh'
            with patch('gusnotebook.tool_installer.target', return_value=(system, arch)), self.download(payload, member, system=system, arch=arch):
                self.installer.install('gh', self.cancel, lambda **values: self.progress.append(values))
            self.assertEqual((self.directory / 'gh').read_bytes(), payload)
            self.assertEqual((self.directory / 'gh').stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(self.directory.iterdir()), [self.directory / 'gh'])
            self.assertFalse((self.directory.parent / 'outside').exists())
            (self.directory / 'gh').unlink()

    def test_bad_checksum_symlink_and_invalid_binary_never_publish(self):
        member = 'gh_1.2.3_macOS_arm64/bin/gh'
        for payload, digest, symlink in [(b'valid bytes', '0' * 64, False),
                                         (b'target', None, True), (b'not an executable', None, False)]:
            with patch('gusnotebook.tool_installer.target', return_value=('macOS','arm64')), self.download(payload, member, digest=digest, symlink=symlink):
                with self.assertRaises(GitError):
                    self.installer.install('gh', self.cancel, lambda **values: None)
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_cancel_after_download_cleans_staging_and_can_retry(self):
        payload = b'#!/bin/sh\nprintf "gh version 1.2.3\\n"\n'
        member = 'gh_1.2.3_macOS_arm64/bin/gh'
        with patch('gusnotebook.tool_installer.target', return_value=('macOS','arm64')), self.download(payload, member):
            def progress(**values):
                if values['state'] == 'downloading':
                    self.cancel.set()
            with self.assertRaises(GitError):
                self.installer.install('gh', self.cancel, progress)
            self.assertEqual(list(self.directory.iterdir()), [])
            self.cancel.clear()
            self.installer.install('gh', self.cancel, lambda **values: None)
            self.assertTrue((self.directory / 'gh').exists())

    def test_existing_installation_is_preserved_and_configured_paths_are_respected(self):
        self.directory.mkdir()
        (self.directory / 'gh').write_bytes(b'existing installation')
        with patch.object(self.installer, '_download') as download:
            self.installer.install('gh', self.cancel, lambda **values: None)
            download.assert_not_called()
        self.assertEqual((self.directory / 'gh').read_bytes(), b'existing installation')
        (self.directory / 'gh').unlink()
        with patch.dict(os.environ, {'GUSNOTEBOOK_GH':'/missing/custom-gh'}):
            with self.assertRaisesRegex(GitError, 'configured by GUSNOTEBOOK_GH'):
                self.installer.install('gh', self.cancel, lambda **values: None)

    def test_managed_tools_are_found_without_path_and_after_new_installer(self):
        self.directory.mkdir()
        for name in ['gh', 'devtunnel']:
            path = self.directory / name
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        with patch('shutil.which', return_value=None), patch('pathlib.Path.home', return_value=self.directory.parent):
            self.assertEqual(git_executable('gh', 'GUSNOTEBOOK_GH', self.directory), str(self.directory / 'gh'))
            self.assertEqual(tunnel_executable(self.directory), str(self.directory / 'devtunnel'))
            restored = ToolInstaller(self.directory)
            try:
                self.assertTrue(all(tool['available'] for tool in restored.snapshot()['tools']))
            finally:
                restored.close()

    def test_platform_mapping_and_unsupported_platform(self):
        for system, machine, expected in [('Darwin','arm64',('macOS','arm64')), ('Darwin','x86_64',('macOS','amd64')),
                                          ('Linux','aarch64',('linux','arm64')), ('Linux','x86_64',('linux','amd64'))]:
            with patch('platform.system', return_value=system), patch('platform.machine', return_value=machine):
                self.assertEqual(target(), expected)
        with patch('platform.system', return_value='Windows'):
            self.assertFalse(self.installer.snapshot()['supported'])


if __name__ == '__main__':
    unittest.main()
