# Design: a self-hosted accountant agent for existing Beancount ledgers

**Status:** Draft, rough idea
**Date:** 2026-10-19

## Goal

Someone who already keeps a Beancount ledger in git clones this repository, deploys one
container, points it at their ledger repository, and then works with an AI accountant:

- "Here is my October statement" (PDF upload) → the agent drafts the entries, proves them
  against the statement, and proposes them for review.
- "Explain my spending trend this year" → the agent queries the ledger and answers.

Fava stays available beside it for the usual reports.

## Non-goals

- **Not a hosted service.** Users clone and deploy their own copy. Nobody but the user
  holds their data or their API key.
- **No central bank-parser catalogue.** Each user is responsible for their own banks. This
  repository ships the framework and some example extractors, not coverage.
- **Not for people new to Beancount.** The user already has a ledger, an account
  structure and conventions; the agent works inside them.

## Architecture

```
user's container (docker compose, or Cloudflare Containers)
├─ fava                  reports, read-only (exists: deploy/cloudflare/)
├─ agent service         Claude Agent SDK, user's own API key
│   ├─ read tools        bean-query, bean-check, read ledger files
│   ├─ write tools       edit files on a work branch only; commit; open a PR
│   └─ document input    PDFs/CSVs given to the model directly, or to a saved extractor
├─ chat UI               a fava extension page (one container, one auth) — see open questions
└─ ledger checkout       git clone of the user's ledger repository, with push access
```

Auth sits in front of everything (Cloudflare Access, or a reverse proxy at home). Neither
fava nor the agent has its own login.

## Key decisions

### 1. The agent never writes to the main branch

Every change goes to a work branch, and the user merges it after reading the diff, either
in the chat UI or as a pull request. Three reasons:

- Accounting errors are quiet. A wrong category or a duplicated row passes `bean-check`.
- Statements are untrusted input. Text inside a PDF can try to instruct the agent
  (prompt injection), and review is the backstop.
- Categorisation is the user's decision. The agent proposes and the user approves.

### 2. Verification, not parsers, is the product

The model can read a statement PDF without a bank-specific parser. Parsers were never what
made a number trustworthy. These identities do that:

- previous balance + activity = new balance
- totals printed on the statement equal the sum of the rows
- each statement's opening balance equals the previous statement's closing balance
- a `balance` assertion, dated the day after the close, passes `bean-check`

So the core is a verification layer that every extraction must pass, whoever produced it:
the model, a saved extractor, or a hand-written one. An extraction that fails is not shown
as a proposal. The failure goes back to the agent.

Per-bank knowledge then accumulates in **the user's ledger repository**, not here.
Examples are column positions, description-to-account rules, and a bank's quirks. The
agent writes them down after a successful run and reuses them next time. The extractors in
`ledger_tools.extract` become examples of the pattern.

Interoperability: follow the beangulp importer interface where it fits, so existing
community importers can feed the same verification layer.

### 3. The user's conventions live in the user's repository

Every ledger has house rules: account naming, how receivables, tags and cash back are
booked, and which flags mean what. The agent reads a conventions file in the ledger
repository (for example `ACCOUNTING.md`, or `AGENTS.md`) at the start of every session,
and the deployment checks that one exists. Nothing ledger-specific is built into the agent.
The plugins in `ledger_tools.plugin` stay opt-in.

### 4. Nothing lives on the container's disk

Cloudflare Containers have ephemeral disk, and a sleeping container loses whatever was not
saved elsewhere. So:

- ledger changes are committed and pushed to the work branch as they are made;
- uploaded documents go to object storage (R2, or a mounted volume at home), and the
  ledger's `document` directives point there;
- a conversation's state that matters ends up in a commit message or the PR description.

### 5. docker compose first, Cloudflare second

Many Beancount users self-host at home. The primary target is `docker compose up` with a
volume. `deploy/cloudflare/` is one deployment of the same image.

## Phases

| phase | capability | risk | done when |
|---|---|---|---|
| 1 | Read-only Q&A: spending trends, balances, "where did X go" via bean-query | low, no writes | answers cite the queries they ran, and the numbers match fava |
| 2 | Statement upload → draft → verify → work branch → user review | medium, the main value | a statement becomes a merged branch with its balance assertion passing |
| 3 | Saved per-bank rules in the user's repository, reused on the next statement | medium | the second statement from a bank needs no new rules |
| 4 | Other front ends (WhatsApp for quick capture, etc.) | depends | only after 1–3 are stable |

Phase 1 alone is useful, and it settles the agent, tool, auth and deployment shape before
anything can write.

Quick capture from a phone ("lunch, 20") is a different job from reconciliation. It can
append provisional entries (flagged `!`) to an inbox file at any time. Reconciliation needs
the statement and the review step.

## Security notes

- The API key and the git token are deployment secrets. The git token is scoped to the one
  ledger repository.
- Bank credentials are never in the container. Statements come in by upload. Downloading
  them stays the user's job in their own browser.
- The agent's tool set is the security boundary: no shell. Its tools are queries, file
  edits under the ledger checkout, and git on non-main branches.
- Treat document text and anything else the model reads from the ledger as data, never as
  instructions.

## Open questions

1. **Chat UI**: a fava extension page (one container, one auth, links straight to fava's
   account pages), or a separate web app (freer UI, a second service)?
2. **Model choice**: the Claude Agent SDK ties the agent to Claude. Is that acceptable, or
   should the tool layer also be exposed as an MCP server, so other agents (Claude Code
   locally, or others) can use it too?
3. **Review surface**: a diff view in the chat UI, or pull requests on the git host? Pull
   requests need a host API token and assume GitHub or a similar host.
4. **First user**: the author's own ledger. That means moving its conventions out of
   CLAUDE.md into the conventions file, so that the agent's behaviour comes from the file
   alone.
5. **Documents**: R2 or a volume. What do `document` directives look like when the files
   are not on a local path?
