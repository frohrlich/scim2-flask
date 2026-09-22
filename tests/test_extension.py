import pytest
from flask import Flask
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
