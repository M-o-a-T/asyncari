#
# Copyright (c) 2018, Matthias Urlichs
#

"""trio-ari client
"""

from trio_ari.client import Client
from trio_swagger11.http_client import AsynchronousHttpClient, ApiKeyAuthenticator
import urllib.parse
import trio
from async_generator import asynccontextmanager

@asynccontextmanager
async def connect(base_url, apps, username, password):
    """Helper method for easily async connecting to ARI.

    :param base_url: Base URL for Asterisk HTTP server (http://localhost:8088/)
    :param apps: the Stasis app(s) to register for.
    :param username: ARI username
    :param password: ARI password
    
    Usage::
        async with trio_ari.connect(base_url, "hello", username, password) as ari:
            async for msg in ari:
                ari.nursery.start_soon(handle_msg, msg)

    """
    host = urllib.parse.urlparse(base_url).netloc.split(':')[0]
    http_client = AsynchronousHttpClient(
        auth=ApiKeyAuthenticator(host, username+':'+password))
    try:
        async with trio.open_nursery() as nursery:
            client = Client(nursery, base_url, apps, http_client)
            async with client:
                try:
                    yield client
                finally:
                    nursery.cancel_scope.cancel()
    finally:
        await http_client.close()
