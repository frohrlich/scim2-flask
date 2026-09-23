from abc import ABC
from abc import abstractmethod
from http import HTTPStatus
from typing import Any

from scim2_models import Resource
from scim2_models import SCIMException
from scim2_models import SearchRequest


class ResourceNotFoundError(SCIMException):
    """Raised by a :class:`ScimStorage` when no resource matches an id.

    :rfc:`RFC7644 §3.12 <7644#section-3.12>`, Table 8, "404 (Not Found)":
    "Specified resource (e.g., User) or endpoint does not exist."
    """

    status = HTTPStatus.NOT_FOUND

    def __init__(self, resource_type: type[Resource[Any]], resource_id: str):
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(detail=f"{resource_type.__name__} {resource_id!r} not found")


class ScimStorage(ABC):
    """Storage backend contract for a SCIM server.

    Subclass this and implement its methods to plug your own storage (SQL,
    LDAP, in-memory, ...) into :class:`~scim2_flask.SCIM2`. Each method
    receives the concrete :class:`~scim2_models.Resource` subclass it
    applies to, so a single storage instance can back several resource
    types.
    """

    @abstractmethod
    def query(
        self, resource_type: type[Resource[Any]], resource_id: str
    ) -> Resource[Any]:
        """Return the resource of ``resource_type`` identified by ``resource_id``.

        :raises ResourceNotFoundError: if no such resource exists.
        """

    @abstractmethod
    def search(
        self, resource_type: type[Resource[Any]], search_request: SearchRequest
    ) -> tuple[int, list[Resource[Any]]]:
        """Return ``(total_results, page)`` matching ``search_request``."""

    @abstractmethod
    def create(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> Resource[Any]:
        """Persist ``resource`` and return the stored representation."""

    @abstractmethod
    def update(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> Resource[Any]:
        """Persist ``resource``, identified by its own ``id``, and return the stored representation.

        Used for both PUT replacements and PATCH modifications: in both
        cases the caller has already produced the resource's full wanted
        state and only asks for it to be saved.

        :raises ResourceNotFoundError: if no resource matches ``resource.id``.
        """

    @abstractmethod
    def delete(self, resource_type: type[Resource[Any]], resource_id: str) -> None:
        """Remove the resource of ``resource_type`` identified by ``resource_id``.

        :raises ResourceNotFoundError: if no such resource exists.
        """
