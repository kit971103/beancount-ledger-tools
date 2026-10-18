"""EQ Bank: Main and Card activity as CSV, the page's default range.

The default is the last 15 days on Main; the files are named "<account no> Details.csv".
"""


def run(page):
    for account in ("main", "ppc"):  # ppc is the EQ Bank Card
        page.goto(f"https://secure.eqbank.ca/accounts/{account}/details")
        page.get_by_role("button", name="Download").click()
        page.wait_for_timeout(2000)
