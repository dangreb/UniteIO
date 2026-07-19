# Releasing UniteIO

UniteIO publishes to PyPI from GitHub Actions using Trusted Publishing. No
long-lived PyPI token is stored in GitHub.

## One-time configuration

### PyPI

While signed in as [`dasgreb`](https://pypi.org/user/dasgreb/), open the PyPI
Publishing page and add a pending GitHub publisher with these values:

| Field | Value |
|---|---|
| PyPI project name | `uniteio` |
| GitHub owner | `dangreb` |
| GitHub repository | `UniteIO` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

The pending publisher creates the PyPI project when the first trusted release
is uploaded successfully.

### GitHub

In the repository settings, create an environment named `pypi`. Adding a
required reviewer is recommended so every publication requires approval.

## Release process

1. Update `project.version` in `pyproject.toml` and the Sphinx `version` and
   `release` values in `docs/conf.py`.
2. Run the local checks:

   ```console
   python -m pytest --cov=uniteio --cov-report=term-missing
   python -m sphinx -W --keep-going -b html docs docs/_build/html
   uv build
   uvx twine check --strict dist/*
   ```

3. Commit and push the release changes.
4. On GitHub, create and publish a release whose tag is `v` followed by the
   exact package version, such as `v0.1.0`.
5. Approve the `pypi` environment deployment if required.
6. Confirm the new release at <https://pypi.org/project/uniteio/>.

The release workflow rejects tags that do not match `project.version`. PyPI
release files are immutable, so correct a failed release by increasing the
version rather than attempting to replace an uploaded file.
