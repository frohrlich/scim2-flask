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
from scim2_models import Bulk
from scim2_models import BulkOperation
from scim2_models import BulkRequest
from scim2_models import BulkResponse
from scim2_models import ChangePassword
from scim2_models import Context
from scim2_models import Error
from scim2_models import ETag
from scim2_models import Extension
from scim2_models import Filter
from scim2_models import InvalidFilterException
from scim2_models import ListResponse
from scim2_models import Meta
from scim2_models import Patch
from scim2_models import PatchOp
from scim2_models import Resource
from scim2_models import ResourceType
from scim2_models import ResponseParameters
from scim2_models import Schema
from scim2_models import SCIMException
from scim2_models import ScimProvider
from scim2_models import SearchRequest
from scim2_models import ServiceProviderConfig
from scim2_models import Sort
from werkzeug.exceptions import Forbidden
from werkzeug.exceptions import HTTPException
from werkzeug.exceptions import NotFound
from werkzeug.exceptions import NotImplemented as HTTPNotImplemented
from werkzeug.exceptions import PreconditionFailed

from .storage import ResourceNotFoundError
from .storage import ScimStorage

EXTENSION_NAME = "scim2"


class PayloadTooLargeException(SCIMException):
    """A bulk job beyond the limits the service provider announces.

    :rfc:`RFC7644 §3.7.4 <7644#section-3.7.4>`: "If either limit is
    exceeded, the service provider MUST return HTTP response code 413
    (Payload Too Large)." No scimType of Table 9 goes with that status, so
    the hierarchy scim2-models exposes is extended with it.
    """

    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def _described_models(
    resource_type: type[Resource[Any]],
) -> list[type[Resource[Any]] | type[Extension]]:
    """Return the bare resource and extensions a model is built from.

    ``User[EnterpriseUser]`` gives ``User`` and ``EnterpriseUser``.
    """
    extensions = list(resource_type.get_extension_models().values())
    if not extensions:
        return [resource_type]
    return [resource_type.__bases__[0], *extensions]


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
        storage: ScimStorage,
        resource_types: list[type[Resource[Any]]],
        app: Flask | None = None,
        *,
        url_prefix: str = "/scim/v2",
    ) -> None:
        if not resource_types:
            raise ValueError("SCIM2 extension requires at least one resource type")

        self.storage = storage
        self.resource_types = list(resource_types)
        self.url_prefix = url_prefix
        # ScimProvider takes the bare resources and extensions a service is
        # built from, and binds them back together with resource types; a
        # model such as User[EnterpriseUser] carries both. RFC7643 §6:
        # "schemaExtensions A list of URIs of the resource type's schema
        # extensions."
        self._resource_type_by_model = {
            resource_type: ResourceType.from_resource(resource_type)
            for resource_type in self.resource_types
        }
        self.provider = ScimProvider(
            models=dict.fromkeys(
                model
                for resource_type in self.resource_types
                for model in _described_models(resource_type)
            ),
            resource_types=self._resource_type_by_model.values(),
        )

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        blueprint = self.create_blueprint()
        app.register_blueprint(blueprint)
        app.extensions[EXTENSION_NAME] = self

    def create_blueprint(self) -> Blueprint:
        blueprint = Blueprint("scim2", __name__, url_prefix=self.url_prefix)

        @blueprint.after_request
        def _set_content_type(response: Response) -> Response:
            response.headers["Content-Type"] = "application/scim+json"
            return response

        @blueprint.after_request
        def _set_etag_header(response: Response) -> Response:
            # RFC7644 §3.14: "When supported, SCIM ETags MUST be specified
            # as an HTTP header and SHOULD be specified within the
            # 'version' attribute contained in the resource's 'meta'
            # attribute."
            data = response.get_json(silent=True)
            if isinstance(data, dict) and (meta := data.get("meta")):
                if version := meta.get("version"):
                    response.headers["ETag"] = version
            return response.make_conditional(request)

        blueprint.register_error_handler(ValidationError, self.handle_validation_error)
        blueprint.register_error_handler(SCIMException, self.handle_scim_exception)
        blueprint.register_error_handler(HTTPException, self.handle_http_exception)

        for resource_type in self.resource_types:
            self._register_resource_routes(blueprint, resource_type)

        self._register_discovery_routes(blueprint)
        self._register_bulk_route(blueprint)

        def me_view() -> Any:
            # RFC7644 §3.11: "A service provider that does NOT support
            # this feature SHOULD respond with HTTP status code 501 (Not
            # Implemented)."
            raise HTTPNotImplemented("/Me is not supported")

        blueprint.add_url_rule(
            "/Me", "me", me_view, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
        )

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

    def get_resource_type(self, resource_type: type[Resource[Any]]) -> ResourceType:
        """Return the SCIM metadata describing ``resource_type``."""
        return self._resource_type_by_model[resource_type]

    def _endpoint(self, resource_type: type[Resource[Any]]) -> str:
        return str(self.get_resource_type(resource_type).endpoint).lstrip("/")

    def _slug(self, resource_type: type[Resource[Any]]) -> str:
        return str(self.get_resource_type(resource_type).id).lower()

    def _resource_union(self) -> Any:
        return reduce(or_, self.resource_types)

    def _check_filter_supported(self, search_request: SearchRequest[Any]) -> None:
        """Refuse a filter the service provider does not announce.

        A storage may not filter at all when ``filter.supported`` is false,
        and :rfc:`RFC7644 §3.4.2.2 <7644#section-3.4.2.2>` forbids ignoring
        the filter: "When specified, only those resources matching the
        filter expression SHALL be returned." The same section gives the
        answer for a filter operation the provider cannot process:
        "Providers MUST decline to filter results if the specified filter
        operation is not recognized and return an HTTP 400 error with a
        "scimType" error of "invalidFilter" and an appropriate
        human-readable response as per Section 3.12."
        """
        config = self.get_service_provider_config().filter
        if search_request.filter and not (config and config.supported):
            raise InvalidFilterException(
                detail="Filtering is not supported by this service provider."
            )

    def _check_if_match(self, resource: Resource[Any]) -> None:
        """:rfc:`RFC7644 §3.14 <7644#section-3.14>`.

        "If the service provider supports versioning of resources, the
        client MAY supply an If-Match header (Section 3.1 of [RFC7232]) for
        PUT and PATCH operations to ensure that the requested operation
        succeeds only if the supplied ETag matches the latest service
        provider resource [...]."
        """
        if_match = request.headers.get("If-Match")
        if not if_match:
            return
        version = resource.meta.version if resource.meta else None
        if version is None:
            return
        tags = [tag.strip() for tag in if_match.split(",")]
        if "*" not in tags and version not in tags:
            raise PreconditionFailed("ETag mismatch")

    def _register_resource_routes(
        self, blueprint: Blueprint, resource_type: type[Resource[Any]]
    ) -> None:
        endpoint = self._endpoint(resource_type)
        slug = self._slug(resource_type)

        def search(search_request: SearchRequest[Any], scim_ctx: Context) -> Any:
            self._check_filter_supported(search_request)
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
                response_parameters=search_request,
            )

        def list_view() -> Any:
            search_request = SearchRequest[resource_type].model_validate(
                request.args.to_dict()
            )
            return search(search_request, Context.RESOURCE_QUERY_RESPONSE)

        def search_view() -> Any:
            search_request = SearchRequest[resource_type].model_validate_json(
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
            created = self.storage.create(resource_type, payload)
            created = self._with_location(resource_type, created)
            return self._resource_response(
                created,
                {
                    "scim_ctx": Context.RESOURCE_CREATION_RESPONSE,
                    "response_parameters": response_parameters,
                },
                HTTPStatus.CREATED,
            )

        def get_view(resource_id: str) -> Any:
            response_parameters = ResponseParameters.model_validate(
                request.args.to_dict()
            )
            resource = self.storage.query(resource_type, resource_id)
            resource = self._with_location(resource_type, resource)
            return self._resource_response(
                resource,
                {
                    "scim_ctx": Context.RESOURCE_QUERY_RESPONSE,
                    "response_parameters": response_parameters,
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

        def replace_view(resource_id: str) -> Any:
            response_parameters = ResponseParameters.model_validate(
                request.args.to_dict()
            )
            original = self.storage.query(resource_type, resource_id)
            self._check_if_match(original)
            payload = resource_type.model_validate_json(
                request.data, scim_ctx=Context.RESOURCE_REPLACEMENT_REQUEST
            )
            # RFC7644 §3.5.1: "immutable If one or more values are already
            # set for the attribute, the input value(s) MUST match, or HTTP
            # status code 400 SHOULD be returned with a "scimType" error code
            # of "mutability". [...] readOnly Any values provided SHALL be
            # ignored." Resource.replace() enforces both in-place on payload,
            # carrying readOnly attributes (id, meta) over from the original.
            payload.replace(original)
            updated = self.storage.update(resource_type, payload)
            updated = self._with_location(resource_type, updated)
            return self._resource_response(
                updated,
                {
                    "scim_ctx": Context.RESOURCE_REPLACEMENT_RESPONSE,
                    "response_parameters": response_parameters,
                },
            )

        def patch_view(resource_id: str) -> Any:
            response_parameters = ResponseParameters.model_validate(
                request.args.to_dict()
            )
            resource = self.storage.query(resource_type, resource_id)
            self._check_if_match(resource)
            patch_op = PatchOp[resource_type].model_validate_json(
                request.data, scim_ctx=Context.RESOURCE_PATCH_REQUEST
            )
            # PatchOp.patch() applies every operation in sequence and mutates
            # ``resource`` in-place; it raises a SCIMException subclass (already
            # handled below) when an operation is invalid or targets an
            # immutable attribute.
            if patch_op.patch(resource):
                resource = self.storage.update(resource_type, resource)
            resource = self._with_location(resource_type, resource)
            return self._resource_response(
                resource,
                {
                    "scim_ctx": Context.RESOURCE_PATCH_RESPONSE,
                    "response_parameters": response_parameters,
                },
            )

        def delete_view(resource_id: str) -> Any:
            if request.headers.get("If-Match"):
                self._check_if_match(self.storage.query(resource_type, resource_id))
            self.storage.delete(resource_type, resource_id)
            return "", HTTPStatus.NO_CONTENT

        blueprint.add_url_rule(
            f"/{endpoint}/<resource_id>",
            f"replace_{slug}",
            replace_view,
            methods=["PUT"],
        )
        blueprint.add_url_rule(
            f"/{endpoint}/<resource_id>", f"patch_{slug}", patch_view, methods=["PATCH"]
        )
        blueprint.add_url_rule(
            f"/{endpoint}/<resource_id>",
            f"delete_{slug}",
            delete_view,
            methods=["DELETE"],
        )

    def _register_discovery_routes(self, blueprint: Blueprint) -> None:
        def _reject_filter() -> None:
            """:rfc:`RFC7644 §4 <7644#section-4>`.

            "Query parameters described in Section 3.4.2, such as
            filtering, sorting, and pagination, SHALL be ignored. If a
            "filter" is provided, the service provider SHOULD respond
            with HTTP status code 403 (Forbidden) to ensure that clients
            cannot incorrectly assume that any matching conditions
            specified in a filter are true."
            """
            if request.args.get("filter"):
                raise Forbidden("Discovery endpoints do not support filtering")

        @blueprint.get("/ServiceProviderConfig")
        def service_provider_config() -> Any:
            _reject_filter()
            return self.get_service_provider_config().model_dump(
                scim_ctx=Context.RESOURCE_QUERY_RESPONSE
            )

        @blueprint.get("/ResourceTypes")
        def list_resource_types() -> Any:
            _reject_filter()
            resource_types = self.provider.resource_types
            response = ListResponse[ResourceType](
                total_results=len(resource_types),
                start_index=1,
                items_per_page=len(resource_types),
                resources=resource_types,
            )
            return response.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE)

        @blueprint.post("/.search")
        def search_root() -> Any:
            resource_union = self._resource_union()
            search_request = SearchRequest[resource_union].model_validate_json(
                request.data, scim_ctx=Context.SEARCH_REQUEST
            )
            self._check_filter_supported(search_request)
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
            response = ListResponse[resource_union](
                total_results=total,
                start_index=search_request.start_index or 1,
                items_per_page=len(resources),
                resources=resources,
            )
            return response.model_dump(
                scim_ctx=Context.SEARCH_RESPONSE,
                response_parameters=search_request,
            )

        @blueprint.get("/ResourceTypes/<name>")
        def get_resource_type_view(name: str) -> Any:
            for resource_type in self.provider.resource_types:
                if resource_type.id == name:
                    return resource_type.model_dump(
                        scim_ctx=Context.RESOURCE_QUERY_RESPONSE
                    )
            raise ResourceNotFoundError(ResourceType, name)

        @blueprint.get("/Schemas")
        def list_schemas() -> Any:
            _reject_filter()
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

    def _register_bulk_route(self, blueprint: Blueprint) -> None:
        # Adapted from the "Bulk jobs" and "POST /Bulk" sections of the
        # scim2-models integration guides:
        # https://scim2-models.readthedocs.io/en/latest/integrations/helpers.html#bulk-jobs
        # https://scim2-models.readthedocs.io/en/latest/integrations/flask.html#post-bulk
        @blueprint.post("/Bulk")
        def bulk() -> Any:
            config = self.get_service_provider_config().bulk
            if config is None or not config.supported:
                # RFC7644 §3.12, Table 8, "501 (Not Implemented)": "Service
                # provider does not support the request operation, e.g.,
                # PATCH."
                raise HTTPNotImplemented("Bulk operations are not supported")
            # RFC7644 §3.7.4: "The service provider MUST define the
            # maximum number of operations and maximum payload size a
            # client may send in a single request. [...] If either limit
            # is exceeded, the service provider MUST return HTTP response
            # code 413 (Payload Too Large)."
            payload_size = request.content_length
            if payload_size is None:  # pragma: no cover
                # A client without Content-Length (e.g. chunked transfer
                # encoding); not reproducible through the test client.
                payload_size = len(request.data)
            if (
                config.max_payload_size is not None
                and payload_size > config.max_payload_size
            ):
                raise PayloadTooLargeException(
                    detail=(
                        "The size of the bulk operation exceeds the "
                        f"maxPayloadSize ({config.max_payload_size})."
                    )
                )

            bulk_request = BulkRequest[self._resource_union()].model_validate_json(
                request.data, scim_ctx=Context.BULK_REQUEST
            )
            operations = bulk_request.operations or []
            # RFC7644 §3.7.4: "If either limit is exceeded, the service
            # provider MUST return HTTP response code 413 (Payload Too
            # Large)."
            if (
                config.max_operations is not None
                and len(operations) > config.max_operations
            ):
                raise PayloadTooLargeException(
                    detail=(
                        "The number of operations exceeds the "
                        f"maxOperations ({config.max_operations})."
                    )
                )

            results = []
            errors = 0
            for operation in operations:
                result = self._run_bulk_operation(operation)
                results.append(result)
                if result.status is not None and result.status < HTTPStatus.BAD_REQUEST:
                    continue
                # RFC7644 §3.7: "The service provider MUST continue
                # performing as many changes as possible and disregard
                # partial failures. The client MAY override this behavior by
                # specifying a value for the "failOnErrors" attribute."
                errors += 1
                if (
                    bulk_request.fail_on_errors
                    and errors >= bulk_request.fail_on_errors
                ):
                    break

            response = BulkResponse[self._resource_union()](operations=results)
            return response.model_dump(scim_ctx=Context.BULK_RESPONSE)

    def _resolve_bulk_target(
        self, path: str
    ) -> tuple[type[Resource[Any]] | None, str | None]:
        """Resolve a bulk operation's ``path`` to the resource type (and id, if any) it targets."""
        model = self.provider.model_for_endpoint(path)
        if model is not None:
            return model, None
        endpoint, _, resource_id = path.rpartition("/")
        return self.provider.model_for_endpoint(endpoint), resource_id

    def _run_bulk_operation(self, operation: BulkOperation[Any]) -> BulkOperation[Any]:
        """Apply one bulk operation and describe its outcome.

        The target is resolved before the operation is applied, so a
        failure still knows its location. :rfc:`RFC7644 §3.7
        <7644#section-3.7>`: "location The resource endpoint URL. REQUIRED
        in a response, except in the event of a POST failure."
        """
        # A union type parameter isn't instantiable (BulkOperation[User |
        # Group] resolves to a Union of the two), and every field a result
        # sets (status, location, version, an Error response) is the same
        # regardless of which concrete resource type the operation
        # targets, so any one resource type parameterizes it.
        result = BulkOperation[self.resource_types[0]](
            method=operation.method, bulk_id=operation.bulk_id
        )
        assert operation.path is not None

        resource_type, resource_id = self._resolve_bulk_target(operation.path)
        if resource_type is None:
            if operation.method != BulkOperation.Method.post:
                # RFC7644 §3.7.3: "A "location" attribute that includes
                # the resource's endpoint MUST be returned for all operations
                # except for failed POST operations (which have no
                # location)." That holds even when no resource type answers
                # the path.
                result.location = url_for(
                    "scim2.not_found",
                    _path=operation.path.lstrip("/"),
                    _external=True,
                )
            result.status = HTTPStatus.NOT_FOUND
            result.response = Error(
                status=HTTPStatus.NOT_FOUND,
                detail=f"{operation.path!r} does not designate a known resource type",
            )
            return result

        if operation.method != BulkOperation.Method.post:
            # RFC7644 §3.7.3: "A "location" attribute that includes the
            # resource's endpoint MUST be returned for all operations
            # except for failed POST operations (which have no
            # location)." So it is set once here, ahead of success or
            # failure, rather than duplicated in every branch below.
            assert resource_id is not None
            result.location = self._resource_location_for_id(resource_type, resource_id)

        try:
            if operation.method == BulkOperation.Method.post:
                created = self.storage.create(resource_type, operation.data)
                created = self._with_location(resource_type, created)
                result.status = HTTPStatus.CREATED
                result.location = created.meta.location if created.meta else None
                result.version = created.meta.version if created.meta else None
                return result

            assert resource_id is not None
            original = self.storage.query(resource_type, resource_id)

            # RFC7644 §3.7: "Version MAY be used if the service provider
            # supports entity-tags (ETags) (Section 2.3 of [RFC7232]) and
            # "method" is "PUT", "PATCH", or "DELETE"."
            current_version = original.meta.version if original.meta else None
            if (
                operation.version is not None
                and current_version is not None
                and operation.version != current_version
            ):
                result.status = HTTPStatus.PRECONDITION_FAILED
                result.response = Error(
                    status=HTTPStatus.PRECONDITION_FAILED, detail="ETag mismatch"
                )
                return result

            if operation.method == BulkOperation.Method.delete:
                self.storage.delete(resource_type, resource_id)
                result.status = HTTPStatus.NO_CONTENT
                return result

            if operation.method == BulkOperation.Method.patch:
                if operation.data.patch(original):
                    original = self.storage.update(resource_type, original)
            else:
                operation.data.replace(original)
                original = self.storage.update(resource_type, operation.data)

            updated = self._with_location(resource_type, original)
            result.status = HTTPStatus.OK
            result.version = updated.meta.version if updated.meta else None
            return result

        except SCIMException as exc:
            result.status = exc.status
            result.response = exc.to_error()
            return result

    # -- Overridable hooks --------------------------------------------

    def get_service_provider_config(self) -> ServiceProviderConfig:
        """Return the server's :class:`~scim2_models.ServiceProviderConfig`.

        Override to advertise the features your :class:`ScimStorage`
        actually supports.
        """
        return ServiceProviderConfig(
            patch=Patch(supported=True),
            bulk=Bulk(supported=False, max_operations=0, max_payload_size=0),
            filter=Filter(supported=False, max_results=None),
            change_password=ChangePassword(supported=False),
            sort=Sort(supported=False),
            etag=ETag(supported=False),
            authentication_schemes=[],
        )

    def resource_location(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> str:
        """Return the canonical URL of ``resource``."""
        assert resource.id is not None
        return self._resource_location_for_id(resource_type, resource.id)

    def _resource_location_for_id(
        self, resource_type: type[Resource[Any]], resource_id: str
    ) -> str:
        slug = self._slug(resource_type)
        return url_for(f"scim2.get_{slug}", resource_id=resource_id, _external=True)

    def _with_location(
        self, resource_type: type[Resource[Any]], resource: Resource[Any]
    ) -> Resource[Any]:
        if resource.meta is None:
            resource.meta = Meta()
        resource.meta.location = self.resource_location(resource_type, resource)
        # RFC7643 §3.1: "resourceType The name of the resource type of the
        # resource." The class name of a model carrying extensions, such as
        # User[EnterpriseUser], is not that name.
        resource.meta.resource_type = self.get_resource_type(resource_type).name
        return resource

    def _resource_response(
        self,
        resource: Resource[Any],
        dump_kwargs: dict[str, Any],
        status: int = HTTPStatus.OK,
    ) -> Response:
        """Build a single-resource response.

        :rfc:`RFC7643 §3.1 <7643#section-3.1>`: "location The URI of the
        resource being returned. This value MUST be the same as the
        "Content-Location" HTTP response header (see Section 3.1.4.2 of
        [RFC7231])."

        :rfc:`RFC7644 §3.3 <7644#section-3.3>`: "The URI of the created
        resource SHALL include, in the HTTP "Location" header and the HTTP
        body, a JSON representation [RFC7159] with the attribute
        "meta.location"."
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
