"""Download statements and exports from the banks: you log in, the script clicks.

Logging in stays manual (passwords, MFA, device prompts), and so does nothing
else: once every bank is logged in, each bank's recorded steps run back to back,
fast enough that no session times out while you deal with the next one.

Runs the installed Chrome (`channel="chrome"`, no browser download) on its own
profile outside the repo, so remembered-device cookies survive between runs and
nothing session-like can be committed. Every download lands in
`documents/_inbox/<bank>/` under the bank's own file name; filing it under
`documents/<account>/YYYY-MM-DD.<desc>.<ext>` happens during reconciliation,
because the date is the last day the document covers, which only its content says.

Usage (from inside the ledger repository):
    ledger-fetch run                  # every bank with recorded steps
    ledger-fetch run scotia eq        # just these
    ledger-fetch record scotia        # record or redo one bank's steps

Which banks to open, and their login pages, come from the ledger's
`ledger_tools.toml`:

    [fetch.login]
    scotia = "https://auth.scotiaonline.scotiabank.com/online"

`record` needs the bank listed there; `run` skips one with no step file.

Recording: the script opens the bank's login page with the Playwright Inspector
paused. Log in first, *then* press Record in the Inspector and click through to
the export, so no credential is ever in the recording. Stop, copy the generated
lines into `ledger_tools/fetch/steps/<bank>.py` (this package) as the body of `def run(page):` and drop
the `page.goto(...)` of the login page. Downloads need no special handling --
every one in the context is saved, whether or not the recording wraps it in
`expect_download`.

A step file that `.fill()`s any literal other than a date is refused, so a
recording started before logging in cannot leak a credential into git.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import BrowserContext, Download, Page, sync_playwright

from ledger_tools import config

STEPS = Path(__file__).with_name("steps")
PROFILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "beancount-fetch-profile"


def root() -> Path:
    return Path(config.ledger_root())


def inbox() -> Path:
    return root() / "documents" / "_inbox"


def login() -> dict[str, str]:
    """`[fetch.login]`: bank -> login page, in the order the tabs open."""
    return config.get("fetch", "login")

# .fill("…") with a non-empty literal that is not a date such as 2026-09-01 or 09/01/2026.
SECRET = re.compile(r"""\.fill\(\s*(['"])(?!\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\1)(?!\1)""")


def launch(p) -> BrowserContext:
    return p.chromium.launch_persistent_context(
        PROFILE, channel="chrome", headless=False, accept_downloads=True, no_viewport=True
    )


def keep_downloads(page: Page, bank: str) -> None:
    def save(d: Download) -> None:
        dest = inbox() / bank / d.suggested_filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        d.save_as(dest)
        print(f"  {bank}: saved {dest.relative_to(root())}")

    page.on("download", save)
    # Popups a recording opens (statement viewers) download too.
    page.on("popup", lambda popup: keep_downloads(popup, bank))


def load_steps(bank: str):
    path = STEPS / f"{bank}.py"
    if not path.exists():
        return None
    src = path.read_text(encoding="utf-8")
    if SECRET.search(src):
        sys.exit(f"{path}: .fill() with a literal value -- re-record after logging in")
    spec = importlib.util.spec_from_file_location(f"steps_{bank}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run


def run(banks: list[str]) -> None:
    todo = [(b, load_steps(b)) for b in banks]
    for b, steps in todo:
        if steps is None:
            print(f"{b}: no steps recorded -- `record {b}` first; skipped")
    todo = [(b, s) for b, s in todo if s]
    if not todo:
        return
    with sync_playwright() as p:
        ctx = launch(p)
        pages = {}
        for b, _ in todo:
            page = ctx.new_page()
            keep_downloads(page, b)
            page.goto(login()[b])
            pages[b] = page
        input(f"Log in to all {len(pages)} tabs, then press Enter here... ")
        failed = []
        for b, steps in todo:
            print(f"{b}:")
            try:
                steps(pages[b])
                pages[b].wait_for_timeout(2000)  # let a last download finish
            except Exception as e:  # one bank's changed page must not stop the rest
                print(f"  {b}: FAILED -- {e}".splitlines()[0])
                failed.append(b)
        if failed:
            input(f"Failed: {', '.join(failed)}. Download those by hand now, then press Enter... ")
        ctx.close()


def record(bank: str) -> None:
    with sync_playwright() as p:
        ctx = launch(p)
        page = ctx.new_page()
        keep_downloads(page, bank)
        page.goto(login()[bank])
        print("Log in, then press Record in the Inspector and click to the export.")
        print(f"Paste the recorded lines into {STEPS / f'{bank}.py'}.")
        STEPS.mkdir(exist_ok=True)
        page.pause()
        ctx.close()


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("run", "record"):
        sys.exit(__doc__)
    banks = args[1:] or (list(login()) if args[0] == "run" else [])
    unknown = [b for b in banks if b not in login()]
    if unknown:
        sys.exit(f"unknown bank(s) {unknown}; [fetch.login] has: {', '.join(login())}")
    if args[0] == "record":
        if len(banks) != 1:
            sys.exit("record takes exactly one bank")
        record(banks[0])
    else:
        run(banks)


if __name__ == "__main__":
    main()
