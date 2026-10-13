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

A record preserves its original measurement context. New evidence receives a
separate record; public API documentation describes the currently supported behavior.
