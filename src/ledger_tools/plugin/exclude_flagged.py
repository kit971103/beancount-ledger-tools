"""Drop whole transactions that carry one of a given set of flags.

The ledger books contractual instalments ahead of their statement under the
`&` flag.  They are real commitments, so
they belong in the file, but they are not yet facts: including them inflates
liabilities and expenses in any report that runs past today.  This plugin
removes them from the stream the reports are built from, while they stay in
the source files unchanged.

Usage, in main.beancount:

    plugin "ledger_tools.plugin.exclude_flagged" "&"

The config is the set of flag characters to drop, as a plain string; commas
and whitespace between them are ignored, so "&", "&?" and "&, ?" all work.
With no config at all the default is "&".

Two things to keep in mind:

  * Put the directive BEFORE any plugin that matches postings across
    transactions -- zerosum above all.  Plugins run in the order they are
    declared, and a scheduled instalment's `Assets:98Transfer` legs
    would otherwise be fed to the matcher and left dangling in
    `Assets:99ZeroSum:Match-Transfer` once its partner is dropped.

  * `balance` assertions are NOT adjusted.  An assertion whose figure was
    derived from a dropped transaction will now fail -- which is the correct
    outcome: a balance may only rest on evidence, and a scheduled entry is
    not evidence.

To see the scheduled entries again, comment the directive out.  That is the
whole switch; the plugin deliberately has no environment variable or other
hidden state, so what a given ledger file means never depends on how it was
invoked.
"""

import collections

from beancount.core import data

__plugins__ = ("exclude_flagged",)

# Flags dropped when the directive is given no config.
DEFAULT_FLAGS = "&"

# Characters allowed as separators inside the config string.
_SEPARATORS = " \t,"

ExcludeFlaggedError = collections.namedtuple(
    "ExcludeFlaggedError", "source message entry"
)


def exclude_flagged(entries, options_map, config=None):
    """Remove every transaction whose flag is in `config`.

    Args:
      entries: a list of directives.
      options_map: the parser's options map (unused).
      config: a string of flag characters to drop, e.g. "&".  Separators in
        _SEPARATORS are ignored.  None or absent means DEFAULT_FLAGS.
    Returns:
      A (entries, errors) pair, with the matching transactions removed.
    """
    flags, errors = _parse_config(config)
    if errors:
        return entries, errors

    kept = []
    for entry in entries:
        if isinstance(entry, data.Transaction):
            if entry.flag in flags:
                continue
            # A posting-level flag is not enough to drop the transaction --
            # that would unbalance it -- and silently keeping it would hide a
            # scheduled leg inside a confirmed entry.  Say so instead.
            for posting in entry.postings:
                if posting.flag in flags:
                    errors.append(
                        ExcludeFlaggedError(
                            entry.meta,
                            "Posting to {} is flagged '{}' inside a "
                            "transaction flagged '{}'; only whole "
                            "transactions can be excluded".format(
                                posting.account, posting.flag, entry.flag
                            ),
                            entry,
                        )
                    )
        kept.append(entry)

    return kept, errors


def _parse_config(config):
    """Turn the directive's config string into a set of flag characters."""
    if config is None:
        config = DEFAULT_FLAGS
    if not isinstance(config, str):
        return set(), [
            ExcludeFlaggedError(
                data.new_metadata("<exclude_flagged>", 0),
                "Config must be a string of flag characters, not {!r}".format(config),
                None,
            )
        ]

    flags = {char for char in config if char not in _SEPARATORS}
    if not flags:
        return set(), [
            ExcludeFlaggedError(
                data.new_metadata("<exclude_flagged>", 0),
                "Config names no flags; either give one (e.g. \"&\") or "
                "remove the plugin directive",
                None,
            )
        ]

    return flags, []
