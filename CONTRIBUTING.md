# Contributing

Thanks for helping. Bug reports, printer and slicer test results, and code
are all welcome.

## Report a bug

1. In the palette open **Settings > Advanced > Run diagnostics** and copy the
   report.
2. Open an [issue](https://github.com/percosys/filamatch/issues) with:
   - what you did, what you expected and what happened;
   - the diagnostics report;
   - the end of the log file (`filamatch.log`, see
     [file locations](docs/how-it-works.md#file-locations)).

Check the report and log for anything private (server addresses, file
paths) before you post them. Filamatch never logs your API key or printer
access codes.

## Test with your printer or slicer

The [roadmap](docs/roadmap.md) needs real hardware: Klipper/Moonraker,
Creality CFS, OctoPrint, Prusa and other slicers. If you can test one,
open an issue and say what you have.

## Change the code

1. Read [development](docs/development.md) (layout, rules, tests).
2. Make a branch for one change.
3. Run `python3 tests/run_all.py` until it passes. Add a test or self-test
   check for what you fix.
4. Open a pull request that says what changed and how you tested it in
   Fusion.

Please keep the style of the file you edit, and never add code that blocks
Fusion's user-interface thread or logs secrets.

By contributing you agree that your contribution is released under the
project's [MIT License](LICENSE).
