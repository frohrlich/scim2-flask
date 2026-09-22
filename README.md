# scim2-flask

A Flask extension to build SCIM 2.0 servers, built on top of
[scim2-models](https://scim2-models.readthedocs.io/). It turns a
[`ScimStorage`](src/scim2_flask/storage.py) you write for your own backend
into a
[RFC7643](https://datatracker.ietf.org/doc/html/rfc7643)/[RFC7644](https://datatracker.ietf.org/doc/html/rfc7644)-compliant
HTTP API.

## Features

- Discovery endpoints: `/ServiceProviderConfig`, `/ResourceTypes`, `/Schemas`
- Full CRUD: `POST`/`GET`/`PUT`/`PATCH`/`DELETE`, with atomic PATCH
  application and validation driven by the SCIM attribute characteristics
  (`mutability`, `returned`, `uniqueness`, ...)
- Search: `GET`/`POST .search` per resource type and at the server root,
  with filtering, sorting and pagination delegated to your storage
- Multiple resource types in a single server (`User`, `Group`, or any
  custom resource), each with its own storage-backed collection
- Resource versioning: conditional `GET` (`If-None-Match` → `304`) and
  conditional writes (`If-Match` → `412`), once your storage populates
  `meta.version`
- Bulk operations (`POST /Bulk`): `failOnErrors`, `maxOperations` and
  `maxPayloadSize` enforcement, per-operation `version` checks
- SCIM-compliant error responses (`application/scim+json`,
  `urn:...:Error` payloads) for validation, protocol and storage errors
- Every capability declared in `ServiceProviderConfig` matches what the
  server actually does; nothing is advertised that isn't implemented

The one part of the bulk RFC left out is resolving `bulkId:xxx`
cross-references between operations of the same job (RFC7644 §3.7.2), same
as the [scim2-models integration
guides](https://scim2-models.readthedocs.io/en/latest/integrations/helpers.html#bulk-jobs)
this project draws from.

## Quickstart

```python
from flask import Flask
from scim2_models import User

from scim2_flask import SCIM2, ScimStorage


class MyStorage(ScimStorage):
    """Implement query/search/create/update/delete against your own storage."""

    ...


app = Flask(__name__)
SCIM2(MyStorage(), [User], app=app)
```

`SCIM2` is a regular Flask extension: pass `app` to the constructor, call
`init_app(app)` later, or use it with the application factory pattern.
Subclass it to override things like `get_service_provider_config()` or
`resource_location()`.

See [`examples/minimal_server.py`](examples/minimal_server.py) for a
runnable example backed by an in-memory `ScimStorage` supporting `User` and
`Group`, filtering, sorting, `userName` uniqueness and ETags:

```console
uv run python examples/minimal_server.py
```

## Installation

```shell
pip install scim2-flask
```

## Conformance

The test suite runs the example server through
[scim2-tester](https://github.com/python-scim/scim2-tester), the official
SCIM conformance checker, in addition to targeted tests for the RFC
behaviors it doesn't cover (versioning, bulk, atomic PATCH, discovery
filtering, ...).

## What's SCIM anyway?

SCIM stands for System for Cross-domain Identity Management, and it is a
provisioning protocol. Provisioning is the action of managing a set of
resources across different services, usually users and groups. SCIM is
often used between Identity Providers and applications in completion of
standards like OAuth2 and OpenID Connect. It allows users and groups
creations, modifications and deletions to be synchronized between
applications.

## Getting help

Questions and bug reports go to the
[issue tracker](https://github.com/python-scim/scim2-flask/issues).

## Contributing

```shell
uv run pytest              # run the test suite
uv run pytest --cov        # with coverage
uv run prek run --all-files --show-diff-on-failure  # style checks
```

## License

scim2-flask is released under the Apache-2.0 license.

scim2-flask is built on top of
[scim2-models](https://github.com/python-scim/scim2-models), one of a
collection of SCIM tools maintained by [Yaal Coop](https://yaal.coop),
including [scim2-client](https://github.com/python-scim/scim2-client),
[scim2-tester](https://github.com/python-scim/scim2-tester) and
[scim2-cli](https://github.com/python-scim/scim2-cli).
