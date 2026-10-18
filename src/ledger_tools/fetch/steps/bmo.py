"""BMO Chequing: every transaction since the last download, as CSV.

"Since last download" is stateful on BMO's side, so a run that fails after the
click still advances it; re-run by hand with "Transactions between these dates".
"""


def run(page):
    page.goto("https://www1.bmo.com/banking/digital/accounts")
    page.get_by_role("link", name="Chequing").first.click()
    page.get_by_role("button", name="Download").first.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("All transactions since last download").check()
    dialog.locator("select[name=format]").select_option("csv")
    dialog.get_by_role("button", name="Download").click()
