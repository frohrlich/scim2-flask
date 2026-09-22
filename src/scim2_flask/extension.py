from __future__ import annotations

from functools import reduce
from http import HTTPStatus
from operator import or_
from typing import Any

from flask import Blueprint
from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from flask import url_for
from pydantic import ValidationError
from scim2_models import AuthenticationScheme
from scim2_models import Bulk
from scim2_models import ChangePassword
from scim2_models import Context
from scim2_models import Error
from scim2_models import ETag
from scim2_models import Filter
from scim2_models import ListResponse
from scim2_models import Meta
from scim2_models import Patch
from scim2_models import Resource
from scim2_models import ResourceType
from scim2_models import ResponseParameters
from scim2_models import Schema
from scim2_models import SCIMException
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import ServiceProviderConfig
from scim2_models import Sort
from werkzeug.exceptions import HTTPException
from werkzeug.exceptions import NotFound
from werkzeug.exceptions import NotImplemented as HTTPNotImplemented

from .storage import ResourceNotFoundError
from .storage import ScimStorage

EXTENSION_NAME = "scim2"


class SCIM2:
    """Flask extension exposing a SCIM 2.0 server backed by a :class:`ScimStorage`.

    Usage::

        storage = MyStorage()
        scim2 = SCIM2(storage, [User])
        scim2.init_app(app)

    Or with the application factory pattern::

        scim2 = SCIM2(storage, [User])


        def create_app():
            app = Flask(__name__)
            scim2.init_app(app)
            return app

    Subclass :class:`SCIM2` and override its methods to customize behavior,
    such as the resource location URL.
    """

    def __init__(
        self,
        storage: ScimStorage | None = None,
        resource_types: list[type[Resource[Any]]] | None = None,
        app: Flask | None = None,
        *,
        url_prefix: str = "/scim/v2",
    ) -> None:
        self.storage = storage
        self.resource_types = list(resource_types) if resource_types else []
        self.url_prefix = url_prefix
        self.provider = ScimProvider(models=self.resource_types)
        self._resource_type_by_model = dict(
            zip(self.resource_types, self.provider.resource_types, strict=True)
        )

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        if self.storage is None:
            raise RuntimeError("SCIM2 extension requires a ScimStorage")
        if not self.resource_types:
            raise RuntimeError("SCIM2 extension requires at least one resource type")

        blueprint = self.create_blueprint()
        app.register_blueprint(blueprint)
        app.extensions[EXTENSION_NAME] = self

    def create_blueprint(self) -> Blueprint:
        blueprint = Blueprint("scim2", __name__, url_prefix=self.url_prefix)

        @blueprint.after_request
        def _set_content_type(response: Response) -> Response:
            response.headers["Content-Type"] = "application/scim+json"
            return response

        blueprint.register_error_handler(ValidationError, self.handle_validation_error)
        blueprint.register_error_handler(SCIMException, self.handle_scim_exception)
        blueprint.register_error_handler(HTTPException, self.handle_http_exception)
        blueprint.register_error_handler(
            ResourceNotFoundError, self.handle_resource_not_found
        )

        for resource_type in self.resource_types:
            self._register_resource_routes(blueprint, resource_type)

        self._register_discovery_routes(blueprint)

        def not_found_view(_path: str) -> Any:
            raise NotFound()

        blueprint.add_url_rule(
            "/<path:_path>",
            "not_found",
            not_found_view,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        )

        return blueprint

    # -- Routing -----------------------------------------------------

    @staticmethod
    def _attribute_filters(response_parameters: ResponseParameters) -> dict[str, Any]:
        return {
            "attributes": response_parameters.attributes,
            "excluded_attributes": response_parameters.excluded_attributes,
        }

    def get_resource_type(self, resource_type: type[Resource[Any]]) -> ResourceType:
        """Return the SCIM metadata describing ``resource_type``."""
        return self._resource_type_by_model[resource_type]

    def _endpoint(self, resource_type: type[Resource[Any]]) -> str:
        return str(self.get_resource_type(resource_type).endpoint).lstrip("/")

    def _resource_union(self) -> Any:
        return reduce(or_, self.resource_types)

    def _register_resource_routes(
        self, blueprint: Blueprint, resource_type: type[Resource[Any]]
    ) -> None:
        endpoint = self._endpoint(resource_type)
        slug = resource_type.__name__.lower()

        def search(search_request: SearchRequest, scim_ctx: Context) -> Any:
            assert self.storage is not None
            total, resources = self.storage.search(resource_type, search_request)
            resources = [
                self._with_location(resource_type, resource) for resource in resources
            ]
            response = ListResponse[resource_type](
                total_results=total,
                start_index=search_request.start_index or 1,
                items_per_page=len(resources),
                resources=resources,
            )
            return response.model_dump(
                scim_ctx=scim_ctx,
                **self._attribute_filters(search_request),
            )

        def list_view() -> Any:
            search_request = SearchRequest.model_validate(request.args.to_dict())
            return search(search_request, Context.RESOURCE_QUERY_RESPONSE)

        def search_view() -> Any:
            search_request = SearchRequest.model_validate_json(
                request.data, scim_ctx=Context.SEARCH_REQUEST
            )
            return search(search_request, Context.SEARCH_RESPONSE)

        def create_view() -> Any:
            response_parameters = ResponseParameters.model_validate(
                request.args.to_dict()
            )
            payload = resource_type.model_validate_json(
                request.data, scim_ctx=Context.RESOURCE_CREATION_REQUEST
            )
            assert self.storage is not None
            created = self.storage.create(resource_type, payload)
            created = self._with_location(resource_type, created)
            return self._resource_response(
                created,
                {
                    "scim_ctx": Context.RESOURCE_CREATION_RESPONSE,
                    **self._attribute_filters(response_parameters),
                },
                HTTPStatus.CREATED,
            )

        def get_view(resource_id: str) -> Any:
            response_parameters = ResponseParameters.model_validate(
                request.args.to_dict()
            )
            assert self.storage is not None
            resource = self.storage.query(resource_type, resource_id)
            resource = self._with_location(resource_type, resource)
            return self._resource_response(
                resource,
                {
                    "scim_ctx": Context.RESOURCE_QUERY_RESPONSE,
                    **self._attribute_filters(response_parameters),
                },
            )

        blueprint.add_url_rule(
            f"/{endpoint}", f"list_{slug}", list_view, methods=["GET"]
        )
        blueprint.add_url_rule(
            f"/{endpoint}", f"create_{slug}", create_view, methods=["POST"]
        )
        blueprint.add_url_rule(
            f"/{endpoint}/<resource_id>", f"get_{slug}", get_view, methods=["GET"]
        )
        blueprint.add_url_rule(
            f"/{endpoint}/.search", f"search_{slug}", search_view, methods=["POST"]
        )

        def not_implemented_view(resource_id: str) -> Any:
            raise HTTPNotImplemented(
                f"{request.method} is not implemented for {resource_type.__name__}"
            )

        blueprint.add_url_rule(
            f"/{endpoint}/<resource_id>",
            f"not_implemented_{slug}",
            not_implemented_view,
            methods=["PUT", "PATCH", "DELETE"],
        )

    def _register_discovery_routes(self, blueprint: Blueprint) -> None:
        @blueprint.get("/ServiceProviderConfig")
        def service_provider_config() -> Any:
            return self.get_service_provider_config().model_dump(
                scim_ctx=Context.RESOURCE_QUERY_RESPONSE
            )

        @blueprint.get("/ResourceTypes")
        def list_resource_types() -> Any:
            resource_types = [self.get_resource_type(rt) for rt in self.resource_types]
            response = ListResponse[ResourceType](
                total_results=len(resource_types),
                start_index=1,
                items_per_page=len(resource_types),
                resources=resource_types,
            )
            return response.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE)

        @blueprint.post("/.search")
        def search_root() -> Any:
            search_request = SearchRequest.model_validate_json(
                request.data, scim_ctx=Context.SEARCH_REQUEST
            )
            assert self.storage is not None
            total = 0
            resources: list[Resource[Any]] = []
            for resource_type in self.resource_types:
                sub_total, sub_resources = self.storage.search(
                    resource_type, search_request
                )
                total += sub_total
                resources.extend(
                    self._with_location(resource_type, resource)
                    for resource in sub_resources
                )
            response = ListResponse[self._resource_union()](
                total_results=total,
                start_index=search_request.start_index or 1,
                items_per_page=len(resources),
                resources=resources,
            )
            return response.model_dump(
                scim_ctx=Context.SEARCH_RESPONSE,
                **self._attribute_filters(search_request),
            )

        @blueprint.get("/ResourceTypes/<name>")
        def get_resource_type_view(name: str) -> Any:
            for resource_type in self.resource_types:
                metadata = self.get_resource_type(resource_type)
                if metadata.id == name:
                    return metadata.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE)
            raise ResourceNotFoundError(ResourceType, name)

        @blueprint.get("/Schemas")
        def list_schemas() -> Any:
            schemas = self.provider.schemas
            response = ListResponse[Schema](
                total_results=len(schemas),
                start_index=1,
                items_per_page=len(schemas),
                resources=schemas,
            )
            return response.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE)

        @blueprint.get("/Schemas/<path:schema_id>")
        def get_schema_view(schema_id: str) -> Any:
            for schema in self.provider.schemas:
                if schema.id == schema_id:
                    return schema.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE)
            raise ResourceNotFoundError(Schema, schema_id)

    # -- Overridable hooks --------------------------------------------

    def get_service_provider_config(self) -> ServiceProviderConfig:
        """Return the server's :class:`~scim2_models.ServiceProviderConfig`.

        Override to advertise the features your :class:`ScimStorage`
        actually supports.
        """
        return ServiceProviderConfig(
            patch=Patch(supported=False),
            bulk=Bulk(supported=False, max_operations=0, max_payload_size=0),
            filter=Filter(supported=False, max_results=200),
            change_password=ChangePassword(supported=False),
            sort=Sort(supported=False),
            etag=ETag(supported=False),
            authentication_schemes=[
                AuthenticationScheme(
                    type=AuthenticationScheme.Type.httpbasic,
                    name="HTTP Basic",
                    description="Authentication via HTTP Basic",
                )
            ],
        )

    def resource_location(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> str:
        """Return the canonical URL of ``resource``."""
        slug = resource_type.__name__.lower()
        return url_for(f"scim2.get_{slug}", resource_id=resource.id, _external=True)

    def _with_location(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> Resource[Any]:
        if resource.meta is None:
            resource.meta = Meta()
        resource.meta.location = self.resource_location(resource_type, resource)
        resource.meta.resource_type = (
            resource.meta.resource_type or resource_type.__name__
        )
        return resource

    def _resource_response(
        self,
        resource: Resource[Any],
        dump_kwargs: dict[str, Any],
        status: int = HTTPStatus.OK,
    ) -> Response:
        """Build a single-resource response.

        Per :rfc:`RFC7643 §3.1 <7643#section-3.1>`, ``meta.location`` MUST
        match the ``Content-Location`` response header; a successful
        creation also gets a ``Location`` header pointing at the new
        resource.
        """
        response = jsonify(resource.model_dump(**dump_kwargs))
        response.status_code = status
        location = resource.meta.location if resource.meta else None
        if location:
            response.headers["Content-Location"] = location
            if status == HTTPStatus.CREATED:
                response.headers["Location"] = location
        return response

    def handle_validation_error(self, error: ValidationError) -> tuple[dict, int]:
        scim_error = Error.from_validation_error(error.errors()[0])
        return scim_error.model_dump(), scim_error.status

    def handle_scim_exception(self, error: SCIMException) -> tuple[dict, int]:
        scim_error = error.to_error()
        return scim_error.model_dump(), scim_error.status

    def handle_http_exception(self, error: HTTPException) -> tuple[dict, int]:
        scim_error = Error(status=error.code, detail=error.description)
        return scim_error.model_dump(), error.code or 500

    def handle_resource_not_found(
        self, error: ResourceNotFoundError
    ) -> tuple[dict, int]:
        scim_error = Error(status=HTTPStatus.NOT_FOUND, detail=str(error))
        return scim_error.model_dump(), HTTPStatus.NOT_FOUND
