import json

import pytest
from scim2_models import EnterpriseUser
from scim2_models import ScimProviderError
from scim2_models import User

from examples.minimal_server import InMemoryStorage
from scim2_flask import SCIM2

CORE = str(User.__schema__)
ENTERPRISE = str(EnterpriseUser.__schema__)


@pytest.fixture
def user_id(client):
    r = client.post(
        "/scim/v2/Users",
        data=json.dumps(
            {
                "schemas": [CORE, ENTERPRISE],
                "userName": "bjensen",
                ENTERPRISE: {"employeeNumber": "42"},
            }
        ),
    )
    assert r.status_code == 201
    return r.get_json()["id"]


def test_extension_attributes_are_stored_and_returned(client, user_id):
    payload = client.get(f"/scim/v2/Users/{user_id}").get_json()
    assert payload["schemas"] == [CORE, ENTERPRISE]
    assert payload[ENTERPRISE] == {"employeeNumber": "42"}


def test_meta_resource_type_is_the_resource_type_name(client, user_id):
    # RFC7643 §3.1: "resourceType [...] The name of the resource type of
    # the resource.", not the name of the model carrying the extension.
    payload = client.get(f"/scim/v2/Users/{user_id}").get_json()
    assert payload["meta"]["resourceType"] == "User"


def test_filter_on_extension_attribute(client, user_id):
    r = client.get(f'/scim/v2/Users?filter={ENTERPRISE}:employeeNumber eq "42"')
    assert [u["id"] for u in r.get_json()["Resources"]] == [user_id]


def test_patch_extension_attribute(client, user_id):
    patch = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "replace", "path": f"{ENTERPRISE}:department", "value": "R&D"}
        ],
    }
    r = client.patch(f"/scim/v2/Users/{user_id}", data=json.dumps(patch))
    assert r.status_code == 200
    assert r.get_json()[ENTERPRISE] == {"employeeNumber": "42", "department": "R&D"}


def test_discovery_announces_the_extension(client):
    # RFC7643 §6: "schemaExtensions [...] A list of URIs of the resource
    # type's schema extensions."
    resource_type = client.get("/scim/v2/ResourceTypes/User").get_json()
    assert resource_type["schema"] == CORE
    assert resource_type["schemaExtensions"] == [
        {"schema": ENTERPRISE, "required": False}
    ]

    schemas = client.get("/scim/v2/Schemas").get_json()["Resources"]
    assert ENTERPRISE in [schema["id"] for schema in schemas]


def test_bare_resource_and_its_extended_model_cannot_both_be_served():
    with pytest.raises(ScimProviderError):
        SCIM2(InMemoryStorage(), [User, User[EnterpriseUser]])
