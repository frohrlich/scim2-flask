"""Minimal SCIM server built with scim2-flask.

Run it with:

    uv run python examples/minimal_server.py

Then, for instance:

    curl -X POST http://localhost:5000/scim/v2/Users \\
        -H "Content-Type: application/scim+json" \\
        -d '{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "bjensen"}'

    curl http://localhost:5000/scim/v2/Users
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from flask import Flask
from scim2_models import Meta, Resource, SearchRequest, User

from scim2_flask import SCIM2, ResourceNotFoundError, ScimStorage

MAX_RESULTS = 50


class InMemoryStorage(ScimStorage):
    """A :class:`ScimStorage` storing resources in a plain dict.

    Inspired by the persistence helpers from the scim2-models integration
    guides: https://scim2-models.readthedocs.io/en/latest/integrations/helpers.html
    A real deployment would replace this with a SQL, LDAP, or any other
    storage backend.
    """

    def __init__(self) -> None:
        self.users: dict[str, User] = {}

    def query(
        self, resource_type: type[Resource[Any]], resource_id: str
    ) -> Resource[Any]:
        try:
            return self.users[resource_id]
        except KeyError:
            raise ResourceNotFoundError(resource_type, resource_id) from None

    def search(
        self, resource_type: type[Resource[Any]], search_request: SearchRequest
    ) -> tuple[int, list[Resource[Any]]]:
        # A real backend would push filtering, sorting and pagination down to
        # its query engine. Here we just paginate the naive way, matching the
        # `page_of` helper from the scim2-models integration guides.
        resources = list(self.users.values())
        start = (search_request.start_index or 1) - 1
        limit = (
            search_request.count if search_request.count is not None else MAX_RESULTS
        )
        stop = start + min(limit, MAX_RESULTS)
        return len(resources), resources[start:stop]

    def create(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> Resource[Any]:
        now = datetime.now(timezone.utc)
        resource.id = str(uuid4())
        resource.meta = Meta(
            resource_type=resource_type.__name__, created=now, last_modified=now
        )
        self.users[resource.id] = resource
        return resource


def create_app() -> Flask:
    app = Flask(__name__)
    scim2 = SCIM2(InMemoryStorage(), [User])
    scim2.init_app(app)
    return app


if __name__ == "__main__":
    create_app().run(debug=True)
