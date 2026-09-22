"""Tests for the POST /Bulk endpoint, RFC7644 §3.7."""

import json

import pytest
from scim2_models import BulkOperation
from scim2_models import BulkRequest
from scim2_models import Context
from scim2_models import PatchOp
from scim2_models import PatchOperation
from scim2_models import SCIMException
from scim2_models import User


def test_bulk_dispatches_operations_by_method(scim_client):
    # RFC7644 §3.7: a bulk job groups independent POST/PUT/PATCH/DELETE
    # operations in a single request.
    create = scim_client.bulk(
        BulkRequest[User](
            operations=[
                BulkOperation[User](
                    method="POST",
                    bulk_id="u1",
                    path="/Users",
                    data=User(user_name="bulk-created"),
                )
            ]
        )
    )
    assert create.operations[0].status == 201
    uid = create.operations[0].location.rsplit("/", 1)[-1]

    modify = scim_client.bulk(
        BulkRequest[User](
            operations=[
                BulkOperation[User](
                    method="PATCH",
                    bulk_id="p1",
                    path=f"/Users/{uid}",
                    data=PatchOp[User](
                        operations=[
                            PatchOperation(
                                op="replace", path="displayName", value="Bulked"
                            )
                        ]
                    ),
                ),
                BulkOperation[User](
                    method="PUT",
                    bulk_id="r1",
                    path=f"/Users/{uid}",
                    data=User(user_name="bulk-created", display_name="Replaced"),
                ),
                BulkOperation[User](
                    method="DELETE", bulk_id="d1", path=f"/Users/{uid}"
                ),
            ]
        )
    )
    assert [op.status for op in modify.operations] == [200, 200, 204]

    with pytest.raises(SCIMException) as exc_info:
        scim_client.query(User, uid)
    assert exc_info.value.status == 404


def test_bulk_rejects_job_exceeding_max_operations(client):
    # RFC7644 §3.7.4: "A job holding more operations than maxOperations is
    # refused whole with a 413."
    operations = [
        {
            "method": "POST",
            "bulkId": f"u{i}",
            "path": "/Users",
            "data": {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": f"bulk{i}",
            },
        }
        for i in range(101)
    ]
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:BulkRequest"],
        "Operations": operations,
    }
    r = client.post("/scim/v2/Bulk", data=json.dumps(body))
    assert r.status_code == 413


def test_bulk_stops_after_fail_on_errors(client):
    # RFC7644 §3.7.3: "failOnErrors" caps the failures a client accepts;
    # the operations past that cap stay undone.
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:BulkRequest"],
        "failOnErrors": 1,
        "Operations": [
            {"method": "DELETE", "bulkId": "bad", "path": "/Users/does-not-exist"},
            {
                "method": "POST",
                "bulkId": "never",
                "path": "/Users",
                "data": {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                    "userName": "unreached",
                },
            },
        ],
    }
    r = client.post("/scim/v2/Bulk", data=json.dumps(body))
    operations = r.get_json()["Operations"]
    assert len(operations) == 1
    assert operations[0]["status"] == "404"


def test_bulk_operation_errors_are_embedded_per_operation(client):
    # A single operation's failure does not fail the whole job (RFC7644
    # §3.7.3); its outcome is reported in its own result instead.
    client.post(
        "/scim/v2/Users",
        data=User(user_name="taken").model_dump_json(
            scim_ctx=Context.RESOURCE_CREATION_REQUEST
        ),
    )

    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:BulkRequest"],
        "Operations": [
            {"method": "DELETE", "bulkId": "unknown-path", "path": "/Bogus/xyz"},
            {
                "method": "POST",
                "bulkId": "dupe",
                "path": "/Users",
                "data": {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                    "userName": "taken",
                },
            },
        ],
    }
    r = client.post("/scim/v2/Bulk", data=json.dumps(body))
    operations = r.get_json()["Operations"]
    assert operations[0]["status"] == "404"
    assert operations[1]["status"] == "409"
    assert operations[1]["response"]["scimType"] == "uniqueness"


def test_bulk_rejects_job_exceeding_max_payload_size(client):
    # RFC7644 §3.7.4: "The service provider MUST define the maximum
    # number of operations and maximum payload size a client may send in
    # a single request. [...] If either limit is exceeded, the service
    # provider MUST return HTTP response code 413."
    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:BulkRequest"],
        "Operations": [
            {
                "method": "POST",
                "bulkId": "u1",
                "path": "/Users",
                "data": {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                    "userName": "x" * 2_000_000,
                },
            }
        ],
    }
    r = client.post("/scim/v2/Bulk", data=json.dumps(body))
    assert r.status_code == 413
    assert "maxPayloadSize" in r.get_json()["detail"]


def test_bulk_stale_operation_version_returns_412(client):
    # RFC7644 §3.7: "version [...] MAY be used if the service provider
    # supports entity-tags (ETags) [...] and 'method' is 'PUT', 'PATCH',
    # or 'DELETE'."
    r = client.post(
        "/scim/v2/Users",
        data=User(user_name="bulk-versioned").model_dump_json(
            scim_ctx=Context.RESOURCE_CREATION_REQUEST
        ),
    )
    uid = r.get_json()["id"]

    body = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:BulkRequest"],
        "Operations": [
            {
                "method": "PUT",
                "bulkId": "v1",
                "path": f"/Users/{uid}",
                "version": 'W/"stale"',
                "data": {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                    "userName": "bulk-versioned",
                    "displayName": "Should Not Stick",
                },
            }
        ],
    }
    r = client.post("/scim/v2/Bulk", data=json.dumps(body))
    operations = r.get_json()["Operations"]
    assert operations[0]["status"] == "412"
    # RFC7644 §3.7.3: "location" is REQUIRED for every response but a
    # failed POST, so it must be present even on this 412.
    assert operations[0]["location"].endswith(f"/Users/{uid}")

    r = client.get(f"/scim/v2/Users/{uid}")
    assert r.get_json().get("displayName") is None
