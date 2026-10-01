"""Git operations on saved workspace files, using the host's Git installation."""

import os
from pathlib import Path
import threading

from .git_auth import GitAuth, executable
from .git_process import Commands, GitError, redact


class GitManager:
    def __init__(self, work, tools_dir=None):
        self.work = str(work)
        self.auth = GitAuth(work, tools_dir=tools_dir)
        self.commands = Commands()
        self.lock = threading.RLock()
        self.job = None
        self.worker = None

    def directory(self, path):
        if not isinstance(path, (str, type(None))) or (path and "\0" in path):
            raise GitError("Choose a project folder")
        directory = Path(path or self.work).expanduser().resolve()
        if directory.is_file():
            directory = directory.parent
        if not directory.is_dir():
            raise GitError("Project folder does not exist")
        return str(directory)

    def git(self, directory, *args, cancel=None, check=True, timeout=15):
        git = executable("git", "GUSNOTEBOOK_GIT")
        if not git:
            raise GitError("Install Git on the computer running this workspace")
        env = self.auth.environment()
        env.update(GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", GIT_LITERAL_PATHSPECS="1", LC_ALL="C")
        result = self.commands.run([git, "-C", directory, *args], env=env, cancel=cancel, timeout=timeout)
        if check and result.returncode:
            error = redact(result.stderr or result.stdout or "Git command failed")[-6000:]
            if any(text in error.lower() for text in ("authentication failed", "could not read username", "could not read password")):
                error += "\nSign in from Accounts, or from Accounts in your local app when sharing through a tunnel."
            raise GitError(error)
        return result

    def root(self, path):
        directory = self.directory(path)
        result = self.git(directory, "rev-parse", "--show-toplevel", check=False)
        if result.returncode:
            return directory, None
        return directory, result.stdout.strip()

    def status(self, path):
        directory, root = self.root(path)
        with self.lock:
            operation = None if self.job is None else {k: v for k, v in self.job.items() if k != "cancel"}
        state = {"directory": directory, "root": root, "auth": self.auth.snapshot(), "operation": operation}
        if root is None:
            return state
        raw = self.git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal").stdout.split("\0")
        changes = []
        index = 0
        while index < len(raw) and raw[index]:
            record = raw[index]
            index += 1
            status, name = record[:2], record[3:]
            old = None
            if "R" in status or "C" in status:
                old = raw[index]
                index += 1
            changes.append({"path": name, "old_path": old, "status": status,
                            "staged": status[0] not in " ?", "unstaged": status[1] != " ",
                            "conflict": "U" in status or status in {"AA", "DD"}})
        branch = self.git(root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
        if not branch:
            branch = "Detached " + self.git(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        upstream = self.git(root, "rev-parse", "--abbrev-ref", "@{upstream}", check=False).stdout.strip()
        counts = self.git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}", check=False).stdout.split()
        state.update(branch=branch, upstream=upstream, ahead=int(counts[0]) if counts else 0,
                     behind=int(counts[1]) if len(counts) == 2 else 0, changes=changes[:3000], truncated=len(changes) > 3000)
        return state

    @staticmethod
    def paths(value):
        if not isinstance(value, list) or not 1 <= len(value) <= 3000:
            raise GitError("Choose files to stage or unstage")
        for path in value:
            if not isinstance(path, str) or not path or "\0" in path or path.startswith("/") or ".." in Path(path).parts:
                raise GitError("Invalid repository path")
        return value

    def diff(self, path, file, staged=False):
        _, root = self.root(path)
        if not root:
            raise GitError("This folder is not a Git repository")
        self.paths([file])
        args = ["diff", "--no-ext-diff", "--no-textconv"] + (["--cached"] if staged else []) + ["--", file]
        text = self.git(root, *args).stdout
        if not text and not staged and self.git(root, "ls-files", "--error-unmatch", "--", file, check=False).returncode:
            target = Path(root) / file
            if target.is_symlink():
                text = "New symlink: " + os.readlink(target)
            elif target.is_file():
                with target.open("rb") as stream:
                    data = stream.read(300001)
                text = "Binary file" if b"\0" in data else "New file\n\n" + data.decode("utf-8", "replace")
        return {"text": text[:300000], "truncated": len(text) > 300000}

    def operate(self, body):
        action = body.get("action")
        if action not in {"init", "stage", "unstage", "commit", "fetch", "pull", "push"}:
            raise GitError("Unknown Git action")
        directory, root = self.root(body.get("path"))
        if not root and action != "init":
            raise GitError("This folder is not a Git repository")
        if action in {"stage", "unstage"}:
            files = self.paths(body.get("files"))
        else:
            files = []
        message = body.get("message", "")
        if action == "commit" and (not isinstance(message, str) or not message.strip() or len(message) > 10000 or "\0" in message):
            raise GitError("Enter a commit message")
        with self.lock:
            if self.commands.closed.is_set():
                raise GitError("GusNotebook is shutting down")
            if self.job and self.job["state"] == "running":
                raise GitError("Wait for the current Git operation to finish")
            job = {"action": action, "root": root or directory, "state": "running", "error": None, "output": "",
                   "cancel": threading.Event()}
            self.job = job
            self.worker = threading.Thread(target=self._operate, args=(job, files, message), daemon=True, name="gusnb-git")
            self.worker.start()

    def _operate(self, job, files, message):
        try:
            root, cancel, action = job["root"], job["cancel"], job["action"]
            args = {"init": ["init"], "stage": ["add", "--", *files], "commit": ["commit", "-m", message],
                    "fetch": ["fetch"], "pull": ["pull", "--ff-only"], "push": ["push"]}.get(action)
            if action == "unstage":
                exists = self.git(root, "rev-parse", "--verify", "HEAD", check=False, cancel=cancel).returncode == 0
                args = ["reset", "-q", "HEAD", "--", *files] if exists else ["rm", "--cached", "--ignore-unmatch", "--", *files]
            result = self.git(root, *args, cancel=cancel, timeout=120)
            with self.lock:
                job.update(state="done", output=redact(result.stdout + result.stderr)[-6000:])
        except (OSError, GitError) as exc:
            with self.lock:
                job.update(state="error", error=redact(exc))
        finally:
            if job["cancel"].is_set():
                with self.lock:
                    job.update(state="canceled", error=None)

    def cancel(self):
        with self.lock:
            if self.job:
                self.job["cancel"].set()

    def close(self):
        self.cancel()
        self.commands.close()
        self.auth.close()
        if self.worker:
            self.worker.join(timeout=3)
