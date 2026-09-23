import pytest
from scim2_models import EnterpriseUser
from scim2_models import SearchRequest
from scim2_models import UniquenessException
from scim2_models import User

from examples.minimal_server import InMemoryStorage
from scim2_flask import ResourceNotFoundError


def test_uniqueness_conflict_on_user_name(scim_client):
    scim_client.create(User[EnterpriseUser](user_name="dupe"))
    with pytest.raises(UniquenessException) as exc_info:
        scim_client.create(User[EnterpriseUser](user_name="dupe"))
    assert exc_info.value.status == 409


def test_search_filters_and_sorts(scim_client):
    scim_client.create(
        User[EnterpriseUser](user_name="charlie", emails=[User.Emails(value="c@x.com")])
    )
    scim_client.create(
        User[EnterpriseUser](
            user_name="alice", emails=[User.Emails(value="a@x.com", primary=True)]
        )
    )
    response = scim_client.query(
        User[EnterpriseUser],
        query_parameters=SearchRequest(filter="userName pr", sort_by="emails"),
    )
    values = [u.emails[0].value for u in response.resources]
    assert values == sorted(values)


def test_sort_puts_resources_without_a_value_last(scim_client):
    scim_client.create(
        User[EnterpriseUser](
            user_name="has-email", emails=[User.Emails(value="a@x.com")]
        )
    )
    scim_client.create(User[EnterpriseUser](user_name="no-email"))
    response = scim_client.query(
        User[EnterpriseUser], query_parameters=SearchRequest(sort_by="emails")
    )
    assert [u.user_name for u in response.resources] == ["has-email", "no-email"]


def test_update_unknown_resource_raises():
    storage = InMemoryStorage()
    with pytest.raises(ResourceNotFoundError):
        storage.update(
            User[EnterpriseUser],
            User[EnterpriseUser](id="does-not-exist", user_name="ghost"),
        )
