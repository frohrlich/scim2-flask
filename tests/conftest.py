import pytest
from scim2_client.engines.werkzeug import TestSCIMClient
from werkzeug.test import Client

from examples.minimal_server import create_app


@pytest.fixture
def scim_client() -> TestSCIMClient:
    app = create_app()
    testclient = Client(app)
    return TestSCIMClient(testclient, scim_prefix="/scim/v2")
