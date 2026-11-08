# Releasing

Every release needs the maintainer's own approval of that one release.

## Once, before the first release

- The `pypi` environment (Settings, Environments) has the maintainer as
  required reviewer, administrators may not bypass it, and only tags `v*`
  may deploy to it. The release workflow stops before building when the
  environment has no required reviewer.
- PyPI has a trusted publisher for the project: owner `taipei49314`,
  repository `checkwash-prove`, workflow `release.yml`, environment `pypi`
  (a pending publisher before the first upload).
- Private vulnerability reporting is on (Settings, Advanced Security), as
  SECURITY.md tells reporters to use it.

## Each release

1. The commit to release is on `main`, and the `tests` workflow is green on
   it on every platform and Python version.
2. `__version__` in `src/checkwash_prove/__init__.py` (the package version
   is set only there) and the headings of `CHANGELOG.md` and the version
   THREATMODEL.md covers name the new version.
3. Push an annotated tag `vX.Y.Z` on that commit.
4. The `release` workflow checks that the tag is annotated, lies on `main`
   and names the version, that the `tests` workflow passed on the commit
   and that the `pypi` environment has a required reviewer; it builds the
   sdist and the wheel once, installs the wheel alone on Linux, Windows and
   macOS and runs a real comparison with it, then waits in the `pypi`
   environment.
5. The maintainer approves the deployment on the run's page (**Review
   deployments**, then **Approve and deploy**). The workflow uploads the two
   files to PyPI by trusted publishing, with attestations, and creates the
   GitHub release with the same files.
6. Read back: the files on PyPI and on the GitHub release have the SHA-256
   that the build job printed.

If a job fails after the upload, use **Re-run failed jobs**, never
**Re-run all jobs**: a rebuild need not be byte for byte the same, and PyPI
refuses a different file under a name it already has. A version on PyPI
cannot be replaced. A broken release is yanked on PyPI and followed by
a new version.
