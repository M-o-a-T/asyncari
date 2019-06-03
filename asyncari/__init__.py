#
# Copyright (c) 2018, Matthias Urlichs
#

"""asyncari client
"""

from asyncari.client import Client
from asyncswagger11.http_client import AsynchronousHttpClient, ApiKeyAuthenticator
import urllib.parse
import anyio
from async_generator import asynccontextmanager

@asynccontextmanager
async def connect(base_url, apps, username, password):
    """Helper method for easily async connecting to ARI.

    :param base_url: Base URL for Asterisk HTTP server (http://localhost:8088/)
    :param apps: the Stasis app(s) to register for.
    :param username: ARI username
    :param password: ARI password
    
    Usage::
        async with asyncari.connect(base_url, "hello", username, password) as ari:
            async for msg in ari:
                await ari.taskgroup.spawn(handle_msg, msg)

    """
    host = urllib.parse.urlparse(base_url).netloc.split(':')[0]
    http_client = AsynchronousHttpClient(
        auth=ApiKeyAuthenticator(host, username+':'+password))
    try:
        async with anyio.create_task_group() as tg:
            client = Client(tg, base_url, apps, http_client)
            async with client:
                try:
                    yield client
                finally:
                    await tg.cancel_scope.cancel()
                pass # end client
            pass # end taskgroup
    finally:
        await http_client.close()
        pass # end
