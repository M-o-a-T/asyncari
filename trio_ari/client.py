#
# Copyright (c) 2018 Matthias Urlichs
#

"""Trio-ified ARI client library.
"""

import json
import urllib
import aioari
import trio_asyncio
import trio
import aioswagger11
import inspect

from functools import partial

import logging
log = logging.getLogger(__name__)

__all__ = ["open_ari_client"]

class Client(aioari.Client):
    """Async ARI Client object.

    :param nursery: the Trio nursery to run our task(s) in.
    :param apps: the Stasis app(s) to register for.
    :param base_url: Base URL for accessing Asterisk.
    :param http_client: HTTP client interface.
    """

    def __init__(self, nursery, base_url, apps, http_client):
        self.nursery = nursery
        self._apps = apps
        url = urllib.parse.urljoin(base_url, "ari/api-docs/resources.json")
        self.swagger = aioswagger11.client.SwaggerClient(
            http_client=http_client, url=url)

    async def __aenter__(self):
        await trio_asyncio.run_asyncio(self.init)
        await self.nursery.start(self._run)
        return self

    async def __aexit__(self, *tb):
        with trio.fail_after(1) as scope:
            scope.shield=True
            await trio_asyncio.run_asyncio(self.close)

    async def _run(self, task_status=trio.TASK_STATUS_IGNORED):
        self._task_status = task_status
        await trio_asyncio.run_asyncio(self.run, self._apps)

    async def _Client__run(self, ws):
        self._task_status.started()
        del self._task_status
        await super()._Client__run(ws)

    async def run_operation(self, oper, **kwargs):
        """Trigger an operation.
        Overrided for Trio.
        """
        if kwargs:
            oper = partial(oper, **kwargs)
        return await trio_asyncio.run_asyncio(oper)

    async def get_resp_text(self, resp):
        """Get the text from a response.
        Overrided for Trio.
        """
        return await trio_asyncio.run_asyncio(resp.text)

    @trio_asyncio.aio2trio
    async def process_ws(self, msg):
        """Process one incoming websocket message. asyncio."""

        listeners = list(self.event_listeners.get(msg['type'], [])) \
                    + list(self.event_listeners.get('*', []))
        for listener in listeners:
            # noinspection PyBroadException
            try:
                callback, args, kwargs = listener
                log.debug("cb_type=%s" % type(callback))
                args = args or ()
                kwargs = kwargs or {}
                cb = callback(msg, *args, **kwargs)
                if inspect.iscoroutine(cb):
                    await cb

            except Exception as e:
                self.exception_handler(e)

