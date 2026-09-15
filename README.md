# scim2-flask

A Flask extension to build SCIM2 servers, built on top of [scim2-models](https://scim2-models.readthedocs.io/).

This is an early work in progress: for now it only supports `User` resources
with `POST /Users` (create) and `GET /Users` / `GET /Users/<id>` (read).

## Quickstart

```python
from flask import Flask
from scim2_models import User

from scim2_flask import SCIM2, ScimStorage


class MyStorage(ScimStorage):
    """Implement query/search/create against your own storage."""

    ...


app = Flask(__name__)
SCIM2(MyStorage(), [User], app=app)
```

See [`examples/minimal_server.py`](examples/minimal_server.py) for a runnable
example backed by an in-memory `ScimStorage`:

```console
uv run python examples/minimal_server.py
```
