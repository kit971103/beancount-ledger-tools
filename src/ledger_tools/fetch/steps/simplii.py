"""Simplii Visa: all available transactions (about 17 months) as CSV."""


def run(page):
    page.goto("https://online.simplii.com/ebm-resources/public/client/web/index.html#/accounts/download")
    page.get_by_label("All available").check()
    page.get_by_role("button", name="Download Transactions").click()
