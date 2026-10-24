"""Fava extension: which commits are being served, and a button to pull the ledger.

The page shows the ledger's checked-out commit and its commit time, and this
package's version and commit.  The container image has no `.git`, so there the
commit comes from `$LEDGER_TOOLS_COMMIT` (set at build or deploy time, see
deploy/); a git install (a ledger pinning a tag) has it in pip's record; in an
editable install it is read from the repository.

    2000-01-01 custom "fava-extension" "ledger_tools.fava_ext.status"

"Pull now" runs the container's `ledger-pull` (deploy/container/ledger-pull.sh),
the same script that re-fetches the ledger every few minutes, and is offered
only where that script and `$LEDGER_REPO` exist -- never on a local working
clone, which it would reset.  fava's `--read-only` refuses every request but
GET, so the endpoint is a GET; pulling is idempotent, and the deployment's
access proxy is in front of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path

from flask import Response
from flask import redirect

from fava.ext import FavaExtensionBase
from fava.ext import extension_endpoint

DIST = "beancount-ledger-tools"
PULL_SCRIPT = "ledger-pull"


@dataclass
class Commit:
    """A checked-out commit; `error` instead when it could not be read."""

    hash: str = ""
    time: str = ""
    error: str = ""


@dataclass
class PullResult:
    """The outcome of the last "Pull now", kept for the page that follows."""

    at: str
    ok: bool
    message: str


# Module-level: fava may rebuild the extension when the pulled files reload.
_last_pull: PullResult | None = None


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                          text=True, check=True, timeout=10).stdout.strip()


def _head(directory: Path) -> Commit:
    try:
        commit_hash, time = _git(directory, "log", "-1", "--format=%H%n%cI").splitlines()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Commit(error=f"not readable as a git checkout ({type(exc).__name__})")
    return Commit(commit_hash, time)


def _installed_commit() -> str:
    """The commit pip recorded for a git install (PEP 610 direct_url.json), if any."""
    try:
        direct_url = json.loads(metadata.distribution(DIST).read_text("direct_url.json") or "{}")
    except (metadata.PackageNotFoundError, ValueError):
        return ""
    return direct_url.get("vcs_info", {}).get("commit_id", "")


def _tools_commit() -> str:
    if commit := os.environ.get("LEDGER_TOOLS_COMMIT") or _installed_commit():
        return commit
    # An editable install: src/ledger_tools/fava_ext/status/ in this repository.
    # Installed anywhere else (a .venv inside the ledger), git would find the
    # enclosing repository and report its commit instead.
    root = Path(__file__).resolve().parents[4]
    try:
        if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            return "unknown"
        commit = _git(root, "rev-parse", "HEAD")
        dirty = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--"],
                               capture_output=True, timeout=10).returncode != 0
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return commit + ("-dirty" if dirty else "")


class Status(FavaExtensionBase):
    """The ledger's and this package's commits, and an on-demand ledger pull."""

    report_title = "Status"

    def ledger_commit(self) -> Commit:
        return _head(Path(self.ledger.beancount_file_path).parent)

    def tools_version(self) -> str:
        try:
            return metadata.version(DIST)
        except metadata.PackageNotFoundError:
            return "unknown"

    def tools_commit(self) -> str:
        return _tools_commit()

    def can_pull(self) -> bool:
        return bool(os.environ.get("LEDGER_REPO")) and shutil.which(PULL_SCRIPT) is not None

    def last_pull(self) -> PullResult | None:
        return _last_pull

    @extension_endpoint
    def pull(self) -> Response:
        """Run ledger-pull, then show the page again with its outcome."""
        global _last_pull
        if not self.can_pull():
            return Response("This deployment has no ledger-pull.", status=404)
        before = self.ledger_commit().hash
        try:
            run = subprocess.run([PULL_SCRIPT], capture_output=True, text=True, timeout=120)
            ok = run.returncode == 0
            output = (run.stdout + run.stderr).strip()
        except subprocess.TimeoutExpired:
            ok, output = False, "ledger-pull timed out"
        if token := os.environ.get("GITHUB_TOKEN"):
            output = output.replace(token, "***")
        if ok and not output:
            output = f"already at {before[:7]}" if before else "done"
        _last_pull = PullResult(datetime.now().astimezone().isoformat(timespec="seconds"),
                                ok, output)
        # This endpoint is extension/Status/pull; "./" is the page.
        return redirect("./")
