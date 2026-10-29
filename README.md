# House Finances

A Streamlit app for a shared house bank account. It imports the bank's
CSV exports, tags each transaction with a person or a purpose, splits
spending between housemates, and shows who has paid in more or less than
their share. All data is in one SQLite file. It runs locally, or on a small
server behind Cloudflare Access (see [Hosting](#hosting)).

## Running it

Python 3.13+, managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                     # install dependencies
uv run streamlit run app.py                 # the app
uv run python ingest.py [exports.csv ...]   # import (no paths: a file picker opens)
uv run pytest                               # tests
```

The database is `finances.db` in the project root. The app and the importer
use the file named by the `HOUSEFIN_DB` environment variable instead if it is
set; the importer's `--db` overrides both. It is created and migrated on
first use.
Re-importing an overlapping export is safe: stored transactions are skipped.
The Import page uploads exports from the browser and does the same.

## Hosting

Hosted, the app sits behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/),
which signs people in, and Cloudflare Tunnel, which carries the traffic to
the server so no port is open. Access decides who gets in; the app decides
what they can do, from the email in Access's signed token
(`housefin/auth.py`):

| Variable | Meaning |
|---|---|
| `HOUSEFIN_ACCESS_TEAM_DOMAIN` | Your Access team domain, e.g. `myhouse.cloudflareaccess.com` |
| `HOUSEFIN_ACCESS_AUD` | The Access application's AUD tag |
| `HOUSEFIN_EDITORS` | Comma-separated emails that can change data; everyone else Access lets in can only look |
| `HOUSEFIN_ROLE` | Without the two Access variables (local use): `editor` (default) or `viewer` |

With both Access variables set, a request without a valid token is refused.
Viewers' database connections are read-only, and pages hide their edit
controls from them. Run Streamlit on `127.0.0.1` so only the tunnel reaches
it.

Step-by-step server setup, backups and updates: [deploy/README.md](deploy/README.md).

## How the money works

**Signs.** Amounts are integer cents. A transaction's amount has the bank's
sign: deposits are positive, withdrawals negative. A person's *share* of a
transaction has their sign: what they owe, so shares of a withdrawal are
positive and shares of a refund negative. A split's shares always add up to
exactly the transaction's amount, negated.

**Tags.** Every transaction gets a person or a purpose:

- A deposit from a person is money they *paid in*. A withdrawal paid to a
  person (a reimbursement) counts against what they paid in.
- A withdrawal with a purpose (Rent, Costco, ...) is *spending*, divided
  between people by a split. A deposit with a purpose is a refund, which
  reduces spending the same way.
- Transfer is a built-in purpose for moving money between the household's own
  accounts. It is left out of every total.

**Rules** remember a description (exact, or a contained piece of text) and
tag matching transactions on import. Hand-tagged transactions are never
changed by a rule.

**Months.** A transaction counts toward its *effective month*: its "applies
to month" if one is set, otherwise the month of its date.

**Splits.** A purpose can have a default split (weights per person). A
transaction starts from it, and anyone not in the house that month is left
out. That scales everyone else's share up, which the app never decides on its
own: the transaction is flagged for review until someone confirms it. With no
default, spending is split evenly between the people in the house. Shares are
rounded down to the cent, and the leftover cents go one each to the people
whose shares were rounded down most, rotating between transactions.

**Rooms and rent.** Who is in the house comes from room assignments: a person
is in the house for a month if they slept in any room on any night of it.
Dates of an assignment are nights, both inclusive. Each room carries a fixed
percent of the rent, and the percents total exactly 100%. Rent is split by
nights: a person's weight is room percent × nights they slept there, and a
month's weights total 100 × its days. If a room was empty for some nights,
the payment gets no split until someone says who covers the empty room's
share (the House or named people). Nobody else's share is scaled up.

Worked example (used by the tests): July 2026 has 31 days and the rent is
$15,728.03. Cy leaves the Attic after 10 July; Moana moves into the Nook on
15 July.

| Room | Percent | Who | Nights | Weight | Share |
|---|---|---|---|---|---|
| Front | 40 | Ann | 31 | 1,240 | $6,291.21 |
| Back | 33.375 | Bob | 31 | 1,034.625 | $5,249.23 |
| Attic | 20 | Cy | 10 | 200 | $1,014.71 |
| Attic | 20 | empty 11–31 Jul | 21 | 420 | to decide |
| Nook | 6.625 | empty 1–14 Jul | 14 | 92.75 | to decide |
| Nook | 6.625 | Moana | 17 | 112.625 | $571.41 |

The weights total 3,100. The people's shares come to $13,126.56, and $2,601.47
waits for someone to cover the empty nights.

**Balances.** For each person: paid in − share of spending. Positive is
credit, negative is owed. The running balance covers all time through the
latest selected month. The Balances page can also group a person's deposits
with the shares they pay for, so a non-zero balance points at the groups that
cause it.

## Code map

| Path | What it holds |
|---|---|
| `app.py` | Home page: counts and how to import |
| `pages/0_Import.py` | Upload bank CSV exports (editors) |
| `deploy/` | Server setup: systemd units, tunnel config, backup script |
| `ingest.py` | Command-line importer, with a file picker |
| `pages/` | One Streamlit page per screen (Import, Categorize, Rules, People, Purposes, Reports, Balances, Rooms) |
| `housefin/db.py` | Schema, migrations, seed data, basic lookups |
| `housefin/dates.py` | Date and month arithmetic; the SQL for a transaction's month |
| `housefin/fmt.py` | Money, percents, months and dates as text, in the browser's locale |
| `housefin/ui.py` | Streamlit helpers: sign-in, connection, month picker, messages, date columns |
| `housefin/auth.py` | Who is signed in (Cloudflare Access token) and their role |
| `housefin/parsers/` | Bank CSV formats (Ally) |
| `housefin/ingest.py` | Import with deduplication and rules |
| `housefin/rules.py` | Remembered rules and applying them |
| `housefin/splits.py` | Share arithmetic, default splits, storing splits |
| `housefin/rent.py` | Rent split by rooms, and the rent editor |
| `housefin/rooms.py`, `rooms_ui.py` | Rooms, assignments, vacancies; the Rooms page's tables |
| `housefin/people.py`, `colors.py` | People, their status and colors |
| `housefin/purposes.py` | Purposes and their default splits |
| `housefin/categorize.py` | Listing and tagging transactions (Categorize page) |
| `housefin/reports.py` | Report totals, monthly averages and CSV |
| `housefin/balances.py` | Balances and the transactions behind them |
| `housefin/matching.py`, `matching_ui.py` | Grouping deposits with shares; its Balances view |
| `tests/` | One test file per module or page; in-memory or temporary databases only |
