import pytest
from flask import Flask
from scim2_models import EnterpriseUser
from scim2_models import MutabilityException
from scim2_models import PatchOp
from scim2_models import PatchOperation
from scim2_models import ResourceType
from scim2_models import Schema
from scim2_models import SCIMException
from scim2_models import SearchRequest
from scim2_models import ServiceProviderConfig
from scim2_models import User

from examples.minimal_server import InMemoryStorage
from scim2_flask import SCIM2


def test_validation_error_returns_scim_error(client):
    # A payload that is not even JSON cannot be built with the SCIM client.
    r = client.post("/scim/v2/Users", data=b"{")
    assert r.status_code == 400
    assert r.get_json()["scimType"] == "invalidSyntax"


def test_me_returns_not_implemented(client):
    # RFC7644 §3.11: "A service provider that does NOT support this
    # feature SHOULD respond with HTTP status code 501 (Not
    # Implemented)." The SCIM client has no call for /Me.
    assert client.get("/scim/v2/Me").status_code == 501


@pytest.mark.parametrize("model", [Schema, ResourceType, ServiceProviderConfig])
def test_discovery_endpoints_reject_filter(scim_client, model):
    # RFC7644 §4: "If a 'filter' is provided, the service provider SHOULD
    # respond with HTTP status code 403 (Forbidden) to ensure that clients
    # cannot incorrectly assume that any matching conditions specified in
    # a filter are true."
    with pytest.raises(SCIMException) as exc_info:
        scim_client.query(
            model, query_parameters=SearchRequest(filter='userName eq "x"')
        )
    assert exc_info.value.status == 403


def test_search_with_no_matches_returns_empty_list(scim_client):
    # RFC7644 §3.4.2: "A query that does not return any matches SHALL
    # return success (HTTP status code 200) with 'totalResults' set to a
    # value of 0."
    response = scim_client.query(
        User[EnterpriseUser],
        query_parameters=SearchRequest(filter='userName eq "nobody-has-this-name"'),
    )
    assert response.total_results == 0


def test_patch_is_all_or_nothing(scim_client):
    # RFC7644 §3.5.2: "A PATCH request, regardless of the number of
    # operations, SHALL be treated as atomic. If a single operation
    # encounters an error condition, the original SCIM resource MUST be
    # restored, and a failure status SHALL be returned." The client would
    # refuse to send a PatchOp targeting the read-only "id", so the payload
    # is sent unchecked to reach the server check.
    created = scim_client.create(User[EnterpriseUser](user_name="atomic"))
    patch = {
        "schemas": [str(PatchOp.__schema__)],
        "Operations": [
            {"op": "replace", "path": "displayName", "value": "Should Not Stick"},
            {"op": "replace", "path": "id", "value": "hacked"},
        ],
    }
    with pytest.raises(MutabilityException) as exc_info:
        scim_client.modify(
            User[EnterpriseUser], created.id, patch, check_request_payload=False
        )
    assert exc_info.value.status == 400

    reloaded = scim_client.query(User[EnterpriseUser], created.id)
    assert reloaded.display_name is None


def test_patch_noop_does_not_bump_last_modified(scim_client):
    # RFC7644 §3.5.2.1 (Add Operation): "If the target location already
    # contains the value specified, no changes SHOULD be made to the
    # resource, and a success response SHOULD be returned. Unless other
    # operations change the resource, this operation SHALL NOT change the
    # modify timestamp of the resource."
    created = scim_client.create(
        User[EnterpriseUser](user_name="noop", display_name="Same")
    )
    patch_op = PatchOp[User[EnterpriseUser]](
        operations=[PatchOperation(op="replace", path="displayName", value="Same")]
    )
    scim_client.modify(User[EnterpriseUser], created.id, patch_op)
    reloaded = scim_client.query(User[EnterpriseUser], created.id)
    assert reloaded.meta.last_modified == created.meta.last_modified


def test_replace_unknown_resource_returns_404(scim_client):
    with pytest.raises(SCIMException) as exc_info:
        scim_client.replace(
            User[EnterpriseUser](id="does-not-exist", user_name="ghost")
        )
    assert exc_info.value.status == 404


def test_per_resource_search_endpoint(scim_client):
    created = scim_client.create(User[EnterpriseUser](user_name="searchable"))
    scim_client.create(User[EnterpriseUser](user_name="other"))
    response = scim_client.search(
        SearchRequest[User[EnterpriseUser]](filter='userName eq "searchable"'),
        url="/Users/.search",
    )
    assert [u.id for u in response.resources] == [created.id]


def test_requires_at_least_one_resource_type():
    with pytest.raises(ValueError):
        SCIM2(InMemoryStorage(), [])


def test_constructor_accepts_app_directly():
    app = Flask(__name__)
    SCIM2(InMemoryStorage(), [User], app=app)
    assert "scim2" in app.blueprints


def test_with_location_builds_meta_when_missing(make_scim_client):
    """`_with_location` must build a `meta` when the storage sets none.

    Unlike `InMemoryStorage`, a real backend may not always set `meta`.
    """

    class BareStorage(InMemoryStorage):
        def query(self, resource_type, resource_id):
            resource = super().query(resource_type, resource_id)
            resource.meta = None
            return resource

    storage = BareStorage()
    created = storage.create(User, User(user_name="bare"))
    app = Flask(__name__)
    SCIM2(storage, [User], app=app)

    reloaded = make_scim_client(app).query(User, created.id)
    assert reloaded.meta.location == f"http://localhost/scim/v2/Users/{created.id}"
