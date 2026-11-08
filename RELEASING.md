# Releasing

Every release needs the maintainer's own approval of that one release.

1. The commit to release is on `main`, and the `tests` workflow is green on
   it on every platform and Python version.
2. `__version__` in `src/checkwash_prove/__init__.py` (the only place the
   version is written) and the heading in `CHANGELOG.md` name the new
   version.
3. Push an annotated tag `vX.Y.Z` on that commit.
4. The `release` workflow checks that the tag names the version and lies on
   `main`, builds the sdist and the wheel once, installs the wheel on Linux,
   Windows and macOS and runs it, then waits in the `pypi` environment.
5. The maintainer approves the deployment on the run's page (**Review
   deployments**, then **Approve and deploy**). The workflow uploads the two
   files to PyPI by trusted publishing, with attestations, and creates the
   GitHub release with the same files.
6. Read back: the files on PyPI and on the GitHub release have the SHA-256
   that the build job printed.

A version on PyPI cannot be replaced. A broken release is yanked on PyPI and
followed by a new version.
