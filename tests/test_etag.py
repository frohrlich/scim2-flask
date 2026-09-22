"""Tests for resource versioning (ETags), RFC7644 §3.14."""

import json

from flask import Flask
from scim2_models import Context
from scim2_models import User
from werkzeug.test import Client

from examples.minimal_server import InMemoryStorage
from scim2_flask import SCIM2


def test_conditional_get_returns_304(client):
    # RFC7644 §3.14: "If the resource has not changed, the service
    # provider simply returns an empty body with a 304 (Not Modified)
    # response code."
    r = client.post(
        "/scim/v2/Users",
        data=User(user_name="etagged").model_dump_json(
            scim_ctx=Context.RESOURCE_CREATION_REQUEST
        ),
    )
    uid = r.get_json()["id"]
    etag = r.headers["ETag"]

    r = client.get(f"/scim/v2/Users/{uid}", headers={"If-None-Match": etag})
    assert r.status_code == 304

    r = client.get(f"/scim/v2/Users/{uid}", headers={"If-None-Match": 'W/"stale"'})
    assert r.status_code == 200


def test_stale_if_match_returns_412(client):
    # RFC7644 §3.14: "the client MAY supply an If-Match header [...] for
    # PUT and PATCH operations to ensure that the requested operation
    # succeeds only if the supplied ETag matches the latest service
    # provider resource." Table 8 maps the failure to 412.
    r = client.post(
        "/scim/v2/Users",
        data=User(user_name="stale").model_dump_json(
            scim_ctx=Context.RESOURCE_CREATION_REQUEST
        ),
    )
    uid = r.get_json()["id"]
    stale_etag = r.headers["ETag"]

    r = client.patch(
        f"/scim/v2/Users/{uid}",
        data=json.dumps(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "displayName", "value": "First"}
                ],
            }
        ),
        headers={"If-Match": stale_etag},
    )
    assert r.status_code == 200

    r = client.patch(
        f"/scim/v2/Users/{uid}",
        data=json.dumps(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "displayName", "value": "Second"}
                ],
            }
        ),
        headers={"If-Match": stale_etag},
    )
    assert r.status_code == 412

    r = client.get(f"/scim/v2/Users/{uid}")
    assert r.get_json()["displayName"] == "First"


def test_if_match_wildcard_bypasses_version_check(client):
    r = client.post(
        "/scim/v2/Users",
        data=User(user_name="wildcard").model_dump_json(
            scim_ctx=Context.RESOURCE_CREATION_REQUEST
        ),
    )
    uid = r.get_json()["id"]

    r = client.delete(f"/scim/v2/Users/{uid}", headers={"If-Match": "*"})
    assert r.status_code == 204


def test_if_match_is_a_noop_without_meta_version():
    """`If-Match` must be ignored, not rejected, when unversioned.

    A storage that never sets `meta.version` doesn't support versioning.
    """

    class UnversionedStorage(InMemoryStorage):
        def create(self, resource_type, resource):
            resource = super().create(resource_type, resource)
            resource.meta.version = None
            return resource

    storage = UnversionedStorage()
    app = Flask(__name__)
    SCIM2(storage, [User], app=app)
    testclient = Client(app)

    r = testclient.post(
        "/scim/v2/Users",
        data=User(user_name="unversioned").model_dump_json(
            scim_ctx=Context.RESOURCE_CREATION_REQUEST
        ),
    )
    uid = r.get_json()["id"]
    assert "ETag" not in r.headers

    r = testclient.delete(f"/scim/v2/Users/{uid}", headers={"If-Match": 'W/"anything"'})
    assert r.status_code == 204
