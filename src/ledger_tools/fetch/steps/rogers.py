"""Rogers Bank Mastercard: the open cycle ("Current transactions") as CSV.

The period list is a native <select id="month-select">; its values are the
cycle closing dates (YYYY-MM-DD) plus `current_transactions`.
"""


def run(page):
    page.goto("https://selfserve.rogersbank.com/transactions")
    page.get_by_role("button", name="Download transactions").click()
    page.locator("#month-select").select_option("current_transactions")
    page.get_by_role("button", name="Download", exact=True).click()
