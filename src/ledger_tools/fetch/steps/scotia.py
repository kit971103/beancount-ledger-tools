"""Scotia Momentum Visa: transactions as CSV, and this year's statements as a zip.

"View" opens a statement in a blob: tab that cannot be saved from script, so
"Download all as PDF" (one zip for the selected year) is the way to the PDF.
"""


def run(page):
    page.goto("https://secure.scotiabank.com/my-accounts")
    page.get_by_role("link", name="Scotia Momentum").first.click()
    page.get_by_role("button", name="Download").first.click()
    page.get_by_text("Download as CSV").click()
    page.get_by_role("link", name="Statements").click()
    page.get_by_role("button", name="Download all as PDF").click()
