#!/usr/bin/env python

"""Example demonstrating ARI channel origination.

"""

#
# Copyright (c) 2013, Digium, Inc.
#
import trio_ari
import trio_asyncio
import trio
import logging
from trio_ari.state import ToplevelChannelState, HangupBridgeState
from trio_ari.model import ChannelExit

from aiohttp.web_exceptions import HTTPError, HTTPNotFound, HTTPBadRequest
from pprint import pprint

import os
ast_url = os.getenv("AST_URL", 'http://localhost:8088/')
ast_username = os.getenv("AST_USER", 'asterisk')
ast_password = os.getenv("AST_PASS", 'asterisk')
ast_app = os.getenv("AST_APP", 'hello')
ast_outgoing = os.getenv("AST_OUTGOING", 'SIP/blink')

class CallState(ToplevelChannelState):
    pass

class CallerState(ToplevelChannelState):
    async def on_start(self):
        async with HangupBridgeState.new(self.client, join_timeout=30) as br:
            await br.add(self.channel)
            await br.dial(endpoint=ast_outgoing, State=CallState)

def on_start(objs, event, client):
    # Don't process our own dial
    if event['args'][0] == 'dialed':
        return
    client.nursery.start_soon(_on_start, objs, event, client)

async def _on_start(objs, event, client):
    """Callback for StasisStart events.

    When an incoming channel starts, put it in the holding bridge and
    originate a channel to connect to it. When that channel answers, create a
    bridge and put both of them into it.

    :param incoming:
    :param event:
    """

    # Answer and put in the holding bridge
    incoming = objs['channel']
    cs = CallerState(incoming)
    await cs.main()

async def main():
    async with trio_ari.connect(ast_url, ast_app, ast_username,ast_password) as client:
        client.on_channel_event('StasisStart', on_start, client)
        async for m in client:
            #print("** EVENT **", m)
            pprint(("** EVENT **", m, vars(m)))

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    try:
        trio_asyncio.run(main)
    except KeyboardInterrupt:
        pass

