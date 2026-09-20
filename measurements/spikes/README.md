# Spike records

One JSON file per measurement taken to settle a question, named
`spike-<subject>-live-<version>.json`.

These sit in a subdirectory rather than beside `measurements/catalog-live-<version>.json`
because the two are different things. The catalog report is the run the catalog currently
stands on, and there is exactly one of it: `tests/test_catalog.py` resolves it by looking for
a single `catalog-live-*.json` directly in `measurements/` and fails on any other count. That
search is neither recursive nor loose, so records kept here cannot disturb it, and unlike the
catalog report they accumulate: a spike answers a question once and the answer keeps its date.

Each record says what was measured, when, against which Live version and which Remote Script
build, what the raw readings were, what the result means, and what it does not cover. Where a
record also went into prose, it names the document and the section, so a reader who finds one
first can reach the other.

A record is written against the evidence of one day. It is not revised when a later
measurement disagrees with it; the later measurement gets a record of its own, and the prose
that cites them says which one it follows.
