"""Install official account CLIs into app-owned, per-user storage."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request
import zipfile

from .git_auth import executable as git_executable
from .git_process import Commands, GitError
from .tunnels import executable as tunnel_executable, TunnelError

LABELS = {"gh": "GitHub CLI", "devtunnel": "Dev Tunnels"}
MAX_DOWNLOAD = 200 * 1024 * 1024
MAX_EXECUTABLE = 128 * 1024 * 1024


def target():
    system = {"Darwin": "macOS", "Linux": "linux"}.get(platform.system())
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(machine)
    if not system or not arch:
        raise GitError("Automatic setup supports macOS and Linux on Intel or ARM64. Install the tools manually on this computer.")
    return system, arch


class ToolInstaller:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.commands = Commands()

    def find(self, name):
        if name == "gh":
            return git_executable("gh", "GUSNOTEBOOK_GH", self.directory)
        try:
            return tunnel_executable(self.directory)
        except TunnelError:
            return None

    def snapshot(self):
        try:
            target()
            error = None
        except GitError as exc:
            error = str(exc)
        return {"supported": error is None, "error": error,
                "tools": [{"name": name, "label": label, "available": bool(self.find(name))}
                          for name, label in LABELS.items()]}

    @staticmethod
    def _check(cancel):
        if cancel.is_set():
            raise GitError("Setup canceled")

    def _download(self, url, destination, cancel, progress, limit=MAX_DOWNLOAD):
        accept = "application/vnd.github+json" if url.startswith("https://api.github.com/") else "application/octet-stream"
        request = urllib.request.Request(url, headers={"User-Agent": "GusNotebook", "Accept": accept})
        deadline = time.monotonic() + 180
        self._check(cancel)
        with urllib.request.urlopen(request, timeout=15) as response, destination.open("wb") as stream:
            if not response.geturl().startswith("https://"):
                raise GitError("The download did not use a secure connection. Retry setup.")
            total = int(response.headers.get("Content-Length", "0"))
            if total > limit:
                raise GitError("The download is larger than expected. Retry setup.")
            received = 0
            while True:
                self._check(cancel)
                if time.monotonic() > deadline:
                    raise GitError("Download timed out. Check your connection and retry setup.")
                chunk = response.read1(128 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > limit:
                    raise GitError("The download is larger than expected. Retry setup.")
                stream.write(chunk)
                progress(state="downloading", received=received, total=total)
            if not received or (total and received != total):
                raise GitError("The download was incomplete. Retry setup.")

    def _release(self, system, arch, temporary, cancel):
        metadata = temporary / "release.json"
        self._download("https://api.github.com/repos/cli/cli/releases/latest", metadata, cancel,
                       lambda **_values: None, limit=2 * 1024 * 1024)
        release = json.loads(metadata.read_text())
        tag = release.get("tag_name", "")
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise GitError("Cannot read the latest GitHub CLI release. Retry setup.")
        suffix = "zip" if system == "macOS" else "tar.gz"
        name = f"gh_{tag[1:]}_{system}_{arch}.{suffix}"
        asset = next((item for item in release.get("assets", []) if item.get("name") == name), {})
        digest = asset.get("digest", "") or ""
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise GitError("The GitHub CLI release is missing its download checksum. Retry setup.")
        # Construct the official asset URL; release metadata cannot select another host or repository.
        return f"https://github.com/cli/cli/releases/download/{tag}/{name}", digest[7:], f"gh_{tag[1:]}_{system}_{arch}/bin/gh"

    def install(self, name, cancel, progress):
        if name not in LABELS:
            raise GitError("Unknown account tool")
        if self.find(name):
            progress(state="available")
            return
        variable = "GUSNOTEBOOK_GH" if name == "gh" else "GUSNOTEBOOK_DEVTUNNEL"
        if os.environ.get(variable):
            raise GitError(f"The executable configured by {variable} is unavailable. Fix that path and retry setup.")
        system, arch = target()
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".install-", dir=self.directory) as directory:
                temporary = Path(directory)
                progress(state="preparing", received=0, total=0)
                if name == "gh":
                    url, digest, member = self._release(system, arch, temporary, cancel)
                    archive_kind = "zip" if system == "macOS" else "tar"
                else:
                    suffix = f"osx-{'x64' if arch == 'amd64' else arch}-zip" if system == "macOS" else f"linux-{'x64' if arch == 'amd64' else arch}"
                    url = "https://aka.ms/TunnelsCliDownload/" + suffix
                    digest, member = None, "devtunnel"
                    archive_kind = "zip" if system == "macOS" else None
                archive = temporary / "download"
                self._download(url, archive, cancel, progress)
                self._check(cancel)
                if digest and hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                    raise GitError("GitHub CLI download checksum did not match. Retry setup.")
                progress(state="installing")
                staged = temporary / name
                if archive_kind == "zip":
                    with zipfile.ZipFile(archive) as package:
                        info = package.getinfo(member)
                        if info.file_size > MAX_EXECUTABLE or (info.external_attr >> 16) & 0o170000 == 0o120000:
                            raise GitError("Unexpected executable in the download. Retry setup.")
                        with package.open(info) as source, staged.open("wb") as output:
                            shutil.copyfileobj(source, output)
                elif archive_kind == "tar":
                    with tarfile.open(archive) as package:
                        info = package.getmember(member)
                        if not info.isfile() or info.size > MAX_EXECUTABLE:
                            raise GitError("Unexpected executable in the download. Retry setup.")
                        with package.extractfile(info) as source, staged.open("wb") as output:
                            shutil.copyfileobj(source, output)
                else:
                    if archive.stat().st_size > MAX_EXECUTABLE:
                        raise GitError("Unexpected executable in the download. Retry setup.")
                    archive.rename(staged)
                self._check(cancel)
                staged.chmod(0o755)
                progress(state="checking")
                try:
                    result = self.commands.run([str(staged), "--version" if name == "gh" else "--help"],
                                               env=dict(os.environ), timeout=15, cancel=cancel)
                except OSError as exc:
                    raise GitError(f"{LABELS[name]} could not run on this computer. Check its system requirements and retry setup.") from exc
                marker = "gh version" if name == "gh" else "Dev Tunnels CLI"
                if result.returncode or marker not in result.stdout:
                    raise GitError(f"{LABELS[name]} could not run on this computer. Retry setup.")
                self._check(cancel)
                if not self.find(name):
                    os.replace(staged, self.directory / name)
                progress(state="available")
        except GitError:
            raise
        except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise GitError(f"Could not install {LABELS[name]}. Check your connection and retry setup.") from exc

    def close(self):
        self.commands.close()
