# Changelog

checkwash-prove is experimental: options, output and exit codes may change in
any version.

## 0.1.0

First release.

- `checkwash-prove BASE..HEAD` (or `BASE...HEAD`, from the merge base) runs a
  range's old and new tests against mutants of the product code from the
  frozen catalogue v0.5-m2, checked by hash, and reports each **protection
  loss**: a concrete fault the old tests caught and the new suite lets
  through, confirmed by running it again alone, with the command that
  replays it.
- An old test that fails on the new code is listed under **needs
  clarification**, never counted as a loss.
- Text and JSON reports; `--list` and `--only` to see and replay single
  mutants; exit codes for `--fail-on-loss`.
- Runs on Linux, Windows and macOS with Python 3.11 to 3.14, against pytest 7
  or later in the interpreter given with `--python`.
