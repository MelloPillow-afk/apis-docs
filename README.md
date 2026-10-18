# API docs

API reference pages generated from each service's OpenAPI spec. One static HTML page per
service: every endpoint with its parameters, a curl example, request body, and responses.

```
specs/<service>/        copy of the service's api-specs/ (never edit by hand)
site/<service>/         generated page (never edit by hand)
site/index.html         list of services
scripts/build_api_docs.py
```

- `make docs` regenerates `site/` from `specs/`.
- `make check` runs the tests and fails if `site/` is out of date. CI runs it.

The build fails when a request or response body has no example in the spec; add the example
to the spec rather than here.

## How specs get here

A service's `.github/workflows/api-docs.yml` copies its `api-specs/` into
`specs/<service>/` and runs `make docs`:

- On its pull requests, it only checks that the docs build.
- On pushes to its `main`, it force-pushes `sync/<service>` here and opens or updates one PR.
  If nothing changed, it opens no PR and closes a stale one.

The service needs the secret `APIS_DOCS_TOKEN`: a fine-grained token for this repository only,
with Contents and Pull requests read/write.

To add a service, copy `videos-service`'s workflow and change the service name.

## Hosting

GitHub Pages serves `site/` publicly at https://mellopillow-afk.github.io/apis-docs/.
`.github/workflows/pages.yml` publishes it on every push to `main`, so merging a sync PR
updates the live docs.
