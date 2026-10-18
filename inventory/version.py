"""Which version of the app is running, and whether GitHub has a newer one.

The running version is read straight from the git checkout's files (the app's
service user can't run git on a checkout it doesn't own). Newer versions are
found with GitHub's public compare API, so no credentials are needed.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CACHE_SECONDS = 600


def _git_dir(app_dir):
    git = app_dir / ".git"
    return git if git.is_dir() else None


def local_version(app_dir=APP_DIR):
    """{"commit": sha or None, "branch": name or None} for the checkout the app runs from."""
    git = _git_dir(app_dir)
    if not git:
        return {"commit": None, "branch": None}
    try:
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return {"commit": None, "branch": None}
    if not head.startswith("ref: "):
        return {"commit": head, "branch": None}  # detached HEAD
    ref = head[len("ref: "):]
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
    loose = git / ref
    if loose.is_file():
        return {"commit": loose.read_text().strip(), "branch": branch}
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return {"commit": parts[0], "branch": branch}
    return {"commit": None, "branch": branch}


def github_repo(app_dir=APP_DIR):
    """"owner/repo" from the checkout's origin remote, if it's on GitHub."""
    git = _git_dir(app_dir)
    if not git or not (git / "config").is_file():
        return None
    config = (git / "config").read_text()
    section = re.search(r'\[remote "origin"\](.*?)(?=\n\[|\Z)', config, re.S)
    if not section:
        return None
    url = re.search(r"^\s*url\s*=\s*(\S+)", section.group(1), re.M)
    if not url:
        return None
    match = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.group(1))
    return f"{match.group(1)}/{match.group(2)}" if match else None


class UpdateChecker:
    """Asks GitHub how far behind the running version is. Results are cached."""

    def __init__(self, app_dir=APP_DIR, fetch=None):
        self.app_dir = app_dir
        self.fetch = fetch or self._fetch
        self.lock = threading.Lock()
        self.cached = None  # (time, commit, result)

    @staticmethod
    def _fetch(url):
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "inventory-tracker"})
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.load(res)

    def check(self, force=False):
        local = local_version(self.app_dir)
        repo = github_repo(self.app_dir)
        if not local["commit"] or not local["branch"] or not repo:
            raise RuntimeError("This copy wasn't installed from GitHub with git, so it can't check for updates.")
        with self.lock:
            if (not force and self.cached and self.cached[1] == local["commit"]
                    and time.monotonic() - self.cached[0] < CACHE_SECONDS):
                return self.cached[2]
        try:
            data = self.fetch(f"https://api.github.com/repos/{repo}/compare/{local['commit']}...{local['branch']}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"Couldn't reach GitHub to check for updates ({e}).")
        commits = data.get("commits") or []
        changes = [
            {"message": (c.get("commit", {}).get("message") or "").splitlines()[0][:200],
             "date": c.get("commit", {}).get("committer", {}).get("date")}
            for c in reversed(commits[-20:])  # newest first
        ]
        result = {
            "available": data.get("status") in ("ahead", "diverged") and data.get("ahead_by", 0) > 0,
            "behind_by": data.get("ahead_by", 0),
            "latest": commits[-1]["sha"] if commits else local["commit"],
            "changes": changes,
        }
        with self.lock:
            self.cached = (time.monotonic(), local["commit"], result)
        return result
