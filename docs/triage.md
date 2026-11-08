# Triage: from an issue to a release

How a GitHub issue becomes work. Each step says what is done when.

## 1. Sort the issue

| Kind | Label | First question |
|---|---|---|
| Something is broken | `bug` | Can we see it with our own printers or the test model? |
| A printer is not supported | `printer support` | Do we have the data from step 2? |
| A new feature | `enhancement` | Does it fit the [roadmap](roadmap.md)? |

Thank the reporter, and say what happens next. If private values are in
the issue (serial numbers, access codes, IP addresses, keys), edit them
out at once.

## 2. Get the data

- **Bug:** the diagnostics report, the end of `filamatch.log`, the steps,
  and the 3MF or project file when the problem is in the slicer.
- **Printer:** the slicer project saved with the printer selected, and the
  printer's status: **Settings > Advanced > Save printer report** in the
  palette writes it with private values removed. The fields we need and why
  are in [design/multi-nozzle.md](design/multi-nozzle.md).

Ask once, with a list. Close the issue after 30 days without an answer;
it can be opened again.

## 3. Turn the data into a test, first

- A printer report becomes a fixture in `tests/fixtures/<source>/`, after a
  second check that no private value is left (the publish script's privacy
  scan also runs on the fixtures).
- A slicer problem becomes a case in `tests/slicer_matrix.json`, or a
  self-test with the smallest file that shows it.
- A new printer gets its entry in `Filamatch/data/printers.json` (see
  [development.md](development.md#add-a-printer)).

The test fails before the fix and passes after it.

## 4. Fix on a branch

One branch per issue. While you work:

```bash
python3 tests/run_all.py --changed
```

Before you merge:

```bash
python3 tests/run_all.py
python3 scripts/slicer_matrix.py compare
```

Nothing BROKEN; CHANGED only where the change is intended.

## 5. Update the docs and the test level

- The changelog's Unreleased section: what changed, what is tested and what
  is not.
- The printer's level in `Filamatch/data/printers.json`, then
  `python3 scripts/printer_docs.py`.
- A printer moves to "validated on a real printer" only when an owner has
  confirmed the full flow: loaded spools, send, slots in the slicer, and the
  print.

## 6. Release and reply

Small fixes ship together. The release notes carry the slicer test matrix.
Reply in the issue with the version that has the fix, and ask the reporter
to confirm. A printer owner who tested a build is thanked in the changelog
if they agree.
