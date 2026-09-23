import pytest
from flask import Flask
from scim2_client.engines.werkzeug import TestSCIMClient
from werkzeug.test import Client

from examples.minimal_server import create_app


@pytest.fixture
def app() -> Flask:
    return create_app()


@pytest.fixture
def client(app: Flask) -> Client:
    return Client(app)


@pytest.fixture
def scim_client(app: Flask, client: Client) -> TestSCIMClient:
    return TestSCIMClient(
        client, scim_prefix="/scim/v2", provider=app.extensions["scim2"].provider
    )
