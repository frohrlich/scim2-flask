import json

import pytest
from flask import Flask
from scim2_models import Context
from scim2_models import PatchOp
from scim2_models import PatchOperation
from scim2_models import SCIMException
from scim2_models import SearchRequest
from scim2_models import User
from werkzeug.test import Client

from examples.minimal_server import InMemoryStorage
from scim2_flask import SCIM2


def test_validation_error_returns_scim_error(client):
    r = client.post("/scim/v2/Users", data=b"{")
    assert r.status_code == 400
    assert r.get_json()["scimType"] == "invalidSyntax"


def test_discovery_endpoints_reject_filter(client):
    # RFC7644 §4: "If a 'filter' is provided, the service provider SHOULD
    # respond with HTTP status code 403 (Forbidden) to ensure that clients
    # cannot incorrectly assume that any matching conditions specified in
    # a filter are true."
    for path in ("/Schemas", "/ResourceTypes", "/ServiceProviderConfig"):
        r = client.get(f"/scim/v2{path}?filter=" + 'userName%20eq%20"x"')
        assert r.status_code == 403, path


def test_search_with_no_matches_returns_empty_list(scim_client):
    # RFC7644 §3.4.2: "A query that does not return any matches SHALL
    # return success (HTTP status code 200) with 'totalResults' set to a
    # value of 0."
    response = scim_client.query(
        User,
        query_parameters=SearchRequest(filter='userName eq "nobody-has-this-name"'),
    )
    assert response.total_results == 0


def test_patch_is_all_or_nothing(client):
    # RFC7644 §3.5.2: "A PATCH request, regardless of the number of
    # operations, SHALL be treated as atomic. If a single operation
    # encounters an error condition, the original SCIM resource MUST be
    # restored, and a failure status SHALL be returned." The client
    # validates PatchOp locally before sending it, so this goes through
    # raw HTTP to reach the server check.
    r = client.post(
        "/scim/v2/Users",
        data=User(user_name="atomic").model_dump_json(
            scim_ctx=Context.RESOURCE_CREATION_REQUEST
        ),
    )
    uid = r.get_json()["id"]

    patch = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "replace", "path": "displayName", "value": "Should Not Stick"},
            {"op": "replace", "path": "id", "value": "hacked"},
        ],
    }
    r = client.patch(f"/scim/v2/Users/{uid}", data=json.dumps(patch))
    assert r.status_code == 400

    r = client.get(f"/scim/v2/Users/{uid}")
    assert r.get_json().get("displayName") is None


def test_patch_noop_does_not_bump_last_modified(scim_client):
    # RFC7644 §3.5.2.1 (Add Operation): "If the target location already
    # contains the value specified, no changes SHOULD be made to the
    # resource, and a success response SHOULD be returned. Unless other
    # operations change the resource, this operation SHALL NOT change the
    # modify timestamp of the resource."
    created = scim_client.create(User(user_name="noop", display_name="Same"))
    patch_op = PatchOp[User](
        operations=[PatchOperation(op="replace", path="displayName", value="Same")]
    )
    scim_client.modify(User, created.id, patch_op)
    reloaded = scim_client.query(User, created.id)
    assert reloaded.meta.last_modified == created.meta.last_modified


def test_replace_unknown_resource_returns_404(scim_client):
    with pytest.raises(SCIMException) as exc_info:
        scim_client.replace(User(id="does-not-exist", user_name="ghost"))
    assert exc_info.value.status == 404


def test_per_resource_search_endpoint(scim_client):
    created = scim_client.create(User(user_name="searchable"))
    scim_client.create(User(user_name="other"))
    response = scim_client.search(
        SearchRequest[User](filter='userName eq "searchable"'), url="/Users/.search"
    )
    assert [u.id for u in response.resources] == [created.id]


def test_init_app_requires_storage_and_resource_types():
    with pytest.raises(RuntimeError):
        SCIM2(None, [User]).init_app(Flask(__name__))
    with pytest.raises(RuntimeError):
        SCIM2(InMemoryStorage(), []).init_app(Flask(__name__))


def test_constructor_accepts_app_directly():
    app = Flask(__name__)
    SCIM2(InMemoryStorage(), [User], app=app)
    assert "scim2" in app.blueprints


def test_with_location_builds_meta_when_missing():
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

    r = Client(app).get(f"/scim/v2/Users/{created.id}")
    assert r.status_code == 200
    assert (
        r.get_json()["meta"]["location"]
        == f"http://localhost/scim/v2/Users/{created.id}"
    )
