# Security

checkwash-prove runs the code of the repository it is pointed at, at two
revisions, many times, with your privileges and no sandbox. That is its job,
not a vulnerability: run it only on code you would run anyway.
[THREATMODEL.md](THREATMODEL.md) says what it does and does not protect.

## Reporting a vulnerability

Report privately, through this repository's **Security** tab
(**Report a vulnerability**), anything in checkwash-prove itself that breaks
a promise in THREATMODEL.md, for example:

- it starts when nobody ran it (on an import, from a hook or a pytest
  plugin entry point);
- it writes your checkout's working tree, index or refs, or changes or
  deletes a file outside the temporary trees it builds;
- it makes a network request of its own, beyond the two that Git makes and
  THREATMODEL.md names;
- it imports the mutation catalogue before checking its hash;
- it exits 1, the loss code, when there was no loss.

What the code under test does is not covered: checkwash-prove runs it with
your privileges on purpose. A result that the tested code steers (a test
that detects checkwash-prove and hides a loss) is a documented limit; open a
public issue for it.

## Supply chain

checkwash-prove has no runtime dependencies. Releases are built by the
`release` workflow from a tag on `main` and uploaded to PyPI by trusted
publishing, with attestations, after the maintainer approves the `pypi`
environment. The GitHub release carries the same files.
