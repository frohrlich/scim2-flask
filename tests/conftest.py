import pytest
from scim2_client.engines.werkzeug import TestSCIMClient
from scim2_models import Group
from scim2_models import ScimProvider
from scim2_models import User
from werkzeug.test import Client

from examples.minimal_server import create_app


@pytest.fixture
def client() -> Client:
    return Client(create_app())


@pytest.fixture
def scim_client(client: Client) -> TestSCIMClient:
    return TestSCIMClient(
        client, scim_prefix="/scim/v2", provider=ScimProvider(models=[User, Group])
    )
