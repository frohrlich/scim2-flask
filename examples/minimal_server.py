r"""Minimal SCIM server built with scim2-flask.

Run it with:

    uv run python examples/minimal_server.py

Then, for instance:

    curl -X POST http://localhost:5000/scim/v2/Users \\
        -H "Content-Type: application/scim+json" \\
        -d '{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "bjensen"}'

    curl http://localhost:5000/scim/v2/Users

    curl -X POST http://localhost:5000/scim/v2/Groups \\
        -H "Content-Type: application/scim+json" \\
        -d '{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "Engineers"}'

    curl http://localhost:5000/scim/v2/Groups
"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import uuid4

from flask import Flask
from scim2_models import ComplexAttribute
from scim2_models import ETag
from scim2_models import Filter
from scim2_models import Group
from scim2_models import Meta
from scim2_models import Resource
from scim2_models import SearchRequest
from scim2_models import ServiceProviderConfig
from scim2_models import Sort
from scim2_models import UniquenessException
from scim2_models import User
from scim2_models.path import AttributeBinding
from scim2_models.path import Path
from scim2_models.path import attribute_host

from scim2_flask import SCIM2
from scim2_flask import ResourceNotFoundError
from scim2_flask import ScimStorage

MAX_RESULTS = 50


# -- sorting: adapted from the scim2-models integration guides
# https://scim2-models.readthedocs.io/en/latest/integrations/helpers.html
def sort_resources(
    resources: list[Resource[Any]],
    sort_by: Path[Any],
    sort_order: "SearchRequest.SortOrder | None" = None,
) -> list[Resource[Any]]:
    """Order resources by an attribute, per :rfc:`RFC7644 §3.4.2.3 <7644#section-3.4.2.3>`.

    :param resources: The SCIM resources to order.
    :param sort_by: The ``sortBy`` query parameter, resolved by the request it
        came from, which names the resource type the endpoint serves.
    :param sort_order: The ``sortOrder`` query parameter, ascending by default.
    """
    # SearchRequest[resource_type] already resolved and validated ``sort_by``
    # against the model at parse time, so it is never unresolvable here.
    resolved = sort_by.resolve()
    descending = sort_order == SearchRequest.SortOrder.descending

    def key(resource: Resource[Any]) -> tuple[bool, Any]:
        value = sort_value(resource, resolved)
        # "String type attributes are case insensitive by default, unless the
        # attribute type is defined as a case-exact string."
        if isinstance(value, str) and not resolved.case_exact:
            value = value.casefold()
        # "if there is no data for the specified sortBy value, they are sorted
        # via the sortOrder parameter, i.e., they are ordered last if ascending
        # and first if descending", which reversing the whole key achieves.
        return (value is None, value if value is not None else "")

    return sorted(resources, key=key, reverse=descending)


def sort_value(resource: Resource[Any], resolved: AttributeBinding) -> Any:
    """Return the single value a resource is ordered by.

    A path crossing a multi-valued attribute designates the sub-attribute of
    every entry, where an order needs one value per resource, so the entry is
    picked first and the sub-attribute read from it.

    :param resource: The resource to read.
    :param resolved: The attribute the ``sortBy`` designates.
    """
    host = attribute_host(resource, resolved)
    value = None if host is None else getattr(host, resolved.field_name, None)
    sub_field_name = resolved.sub_field_name

    if resolved.is_multivalued:
        entries = value or []
        # "resources are sorted by the value of the primary attribute, if any,
        # or else the first value in the list, if any."
        primary = next(
            (entry for entry in entries if getattr(entry, "primary", None)), None
        )
        value = primary if primary is not None else (entries[0] if entries else None)
        if sub_field_name is None and isinstance(value, ComplexAttribute):
            # RFC7643 §2.4 holds the significant value of a complex entry in a
            # ``value`` sub-attribute, where a scalar entry is the value itself.
            sub_field_name = "value"

    if value is None or sub_field_name is None:
        return value
    return getattr(value, sub_field_name, None)


# -- versioning: adapted from make_etag() in the scim2-models integration
# guides https://scim2-models.readthedocs.io/en/latest/integrations/helpers.html
def make_version(resource: Resource[Any]) -> str:
    """Compute a weak ETag from a resource's content, per :rfc:`RFC7644 §3.14 <7644#section-3.14>`.

    ``meta`` is excluded so that touching only bookkeeping (e.g.
    ``lastModified``) without a real content change still yields the same
    version.
    """
    content = resource.model_dump(mode="json", exclude={"meta"}, scim_ctx=None)
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    return f'W/"{digest[:16]}"'


class InMemoryStorage(ScimStorage):
    """A :class:`ScimStorage` storing resources in a plain dict per resource type.

    Inspired by the persistence helpers from the scim2-models integration
    guides: https://scim2-models.readthedocs.io/en/latest/integrations/helpers.html
    A real deployment would replace this with a SQL, LDAP, or any other
    storage backend.
    """

    def __init__(self) -> None:
        self.resources: dict[type[Resource[Any]], dict[str, Resource[Any]]] = (
            defaultdict(dict)
        )

    def query(
        self, resource_type: type[Resource[Any]], resource_id: str
    ) -> Resource[Any]:
        try:
            return self.resources[resource_type][resource_id]
        except KeyError:
            raise ResourceNotFoundError(resource_type, resource_id) from None

    def search(
        self, resource_type: type[Resource[Any]], search_request: SearchRequest
    ) -> tuple[int, list[Resource[Any]]]:
        resources = list(self.resources[resource_type].values())
        if search_request.filter:
            resources = [r for r in resources if search_request.filter.match(r)]
        if search_request.sort_by:
            resources = sort_resources(
                resources, search_request.sort_by, search_request.sort_order
            )
        start = search_request.start_index_0 or 0
        count = (
            search_request.count if search_request.count is not None else MAX_RESULTS
        )
        stop = start + min(count, MAX_RESULTS)
        return len(resources), resources[start:stop]

    def create(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> Resource[Any]:
        self._check_user_name_unique(resource_type, resource)
        now = datetime.now(timezone.utc)
        resource.id = str(uuid4())
        resource.meta = Meta(
            resource_type=resource_type.__name__, created=now, last_modified=now
        )
        resource.meta.version = make_version(resource)
        self.resources[resource_type][resource.id] = resource
        return resource

    def update(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> Resource[Any]:
        store = self.resources[resource_type]
        if resource.id not in store:
            raise ResourceNotFoundError(resource_type, resource.id)
        self._check_user_name_unique(resource_type, resource)
        created = store[resource.id].meta.created if store[resource.id].meta else None
        resource.meta = Meta(
            resource_type=resource_type.__name__,
            created=created,
            last_modified=datetime.now(timezone.utc),
        )
        resource.meta.version = make_version(resource)
        store[resource.id] = resource
        return resource

    def _check_user_name_unique(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> None:
        """Enforce the ``Uniqueness.global_`` scim2-models declares on ``User.userName``."""
        user_name = getattr(resource, "user_name", None)
        if user_name is None:
            return
        for existing in self.resources[resource_type].values():
            if existing.id != resource.id and existing.user_name == user_name:
                raise UniquenessException(
                    attribute="userName",
                    value=user_name,
                    detail=f"userName {user_name!r} is already taken",
                )

    def delete(self, resource_type: type[Resource[Any]], resource_id: str) -> None:
        try:
            del self.resources[resource_type][resource_id]
        except KeyError:
            raise ResourceNotFoundError(resource_type, resource_id) from None


class MinimalSCIM2(SCIM2):
    """Advertise the capabilities :class:`InMemoryStorage` actually supports."""

    def get_service_provider_config(self) -> ServiceProviderConfig:
        config = super().get_service_provider_config()
        config.filter = Filter(supported=True, max_results=MAX_RESULTS)
        config.sort = Sort(supported=True)
        config.etag = ETag(supported=True)
        return config


def create_app() -> Flask:
    app = Flask(__name__)
    scim2 = MinimalSCIM2(InMemoryStorage(), [User, Group])
    scim2.init_app(app)
    return app


if __name__ == "__main__":  # pragma: no cover
    create_app().run(debug=True)
