"""Features the default ServiceProviderConfig does not announce are refused."""

import pytest
from flask import Flask
from scim2_models import BulkOperation
from scim2_models import BulkRequest
from scim2_models import InvalidFilterException
from scim2_models import SCIMException
from scim2_models import SearchRequest
from scim2_models import User

from examples.minimal_server import InMemoryStorage
from scim2_flask import SCIM2


@pytest.fixture
def scim_client(make_scim_client):
    app = Flask(__name__)
    SCIM2(InMemoryStorage(), [User], app=app)
    return make_scim_client(app)


def test_listing_without_filter_is_served(scim_client):
    created = scim_client.create(User(user_name="bjensen"))
    response = scim_client.query(User)
    assert [u.id for u in response.resources] == [created.id]


def test_unsupported_filter_is_refused_on_resource_endpoint(scim_client):
    # RFC7644 §3.4.2.2: "When specified, only those resources matching the
    # filter expression SHALL be returned."
    with pytest.raises(InvalidFilterException) as exc_info:
        scim_client.query(
            User, query_parameters=SearchRequest(filter='userName eq "bjensen"')
        )
    assert exc_info.value.status == 400


def test_unsupported_filter_is_refused_on_resource_search(scim_client):
    with pytest.raises(InvalidFilterException):
        scim_client.search(
            SearchRequest[User](filter='userName eq "bjensen"'), url="/Users/.search"
        )


def test_unsupported_filter_is_refused_on_root_search(scim_client):
    with pytest.raises(InvalidFilterException):
        scim_client.search(SearchRequest(filter='userName eq "bjensen"'))


def test_unsupported_bulk_is_refused(scim_client):
    # RFC7644 §3.12, Table 8, "501 (Not Implemented)": "Service provider does
    # not support the request operation, e.g., PATCH."
    bulk_request = BulkRequest[User](
        operations=[
            BulkOperation[User](
                method="POST", bulk_id="u1", path="/Users", data=User(user_name="x")
            )
        ]
    )
    with pytest.raises(SCIMException) as exc_info:
        scim_client.bulk(bulk_request)
    assert exc_info.value.status == 501
    assert exc_info.value.detail == "Bulk operations are not supported"
