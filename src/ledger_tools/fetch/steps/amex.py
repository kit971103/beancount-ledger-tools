"""Amex Cobalt: the open cycle's activity as CSV.

The open cycle only exports the basic format (Date, Date Processed, Description,
Amount) even with "additional details" ticked; the Reference column comes only
with a closed statement's period.
"""


def run(page):
    page.goto("https://global.americanexpress.com/activity/recent")
    page.get_by_role("button", name="Download", exact=True).first.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("CSV").check()
    dialog.get_by_role("button", name="Download").click()
