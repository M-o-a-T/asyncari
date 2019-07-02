#!/usr/bin/python3

"""Example demonstrating ARI channel origination.

"""

#
# Copyright (c) 2013, Digium, Inc.
#
import asyncari
from asyncari.util import mayNotExist
import anyio
import logging

from asks.errors import AsksException  # HTTPNotFound, HTTPBadRequest
from pprint import pprint

import os
ast_host = os.getenv("AST_HOST", 'localhost')
ast_port = int(os.getenv("AST_ARI_PORT", 8088))
ast_url = os.getenv("AST_URL", 'http://%s:%d/'%(ast_host,ast_port))
ast_username = os.getenv("AST_USER", 'asterisk')
ast_password = os.getenv("AST_PASS", 'asterisk')
ast_app = os.getenv("AST_APP", 'hello')
ast_outgoing = os.getenv("AST_OUTGOING", 'SIP/blink')

client = None

holding_bridge = None
async def setup():
    global holding_bridge
    #
    # Find (or create) a holding bridge.
    #
    bridges = [b for b in (await client.bridges.list())
            if b.json['bridge_type'] == 'holding']
    if bridges:
        holding_bridge = bridges[0]
        print ("Using bridge %s" % holding_bridge.id)
    else:
        holding_bridge = await client.bridges.create(type='holding')
        print ("Created bridge %s" % holding_bridge.id)


async def safe_hangup(channel):
    """Hangup a channel, ignoring 404 errors.

    :param channel: Channel to hangup.
    """
    with mayNotExist:
        await channel.hangup()


def on_start(objs, event, n):
    n.start_soon(_on_start, objs, event)
async def _on_start(objs, event):
    """Callback for StasisStart events.

    When an incoming channel starts, put it in the holding bridge and
    originate a channel to connect to it. When that channel answers, create a
    bridge and put both of them into it.

    :param incoming:
    :param event:
    """
    # Don't process our own dial
    if event['args'][0] == 'dialed':
        return

    # Answer and put in the holding bridge
    incoming = objs['channel']
    await incoming.answer()
    p = await incoming.play(media="sound:queue-youarenext")
    print("PLAY:",p)
    await p.wait_done()
    try:
        h = await holding_bridge.addChannel(channel=incoming.id)
    except HTTPBadRequest:
        try:
            await incoming.hangup()
        except HTTPNotFound:
            pass
        return
    print("HOLD:",h)

    # Originate the outgoing channel
    outgoing = await client.channels.originate(
        endpoint=ast_outgoing, app=ast_app, appArgs=["dialed","123"])
    print("OUT:",outgoing)

    # If the incoming channel ends, hangup the outgoing channel
    incoming.on_event('StasisEnd', lambda *args: safe_hangup(outgoing))
    # and vice versa. If the endpoint rejects the call, it is destroyed
    # without entering Stasis()
    outgoing.on_event('ChannelDestroyed',
                      lambda *args: safe_hangup(incoming))

    async def outgoing_on_start(event, channel):
        """Callback for StasisStart events on the outgoing channel

        :param channel: Outgoing channel.
        :param event: Event.
        """
        # Create a bridge, putting both channels into it.
        print("Bridging",channel)
        bridge = await client.bridges.create(type='mixing')
        await outgoing.answer()
        await bridge.addChannel(channel=[incoming.id, outgoing.id])
        print("Bridged",incoming,outgoing)
        # Clean up the bridge when done
        outgoing.on_event('StasisEnd', lambda *args: bridge.destroy())

    outgoing.on_event('StasisStart', outgoing_on_start, outgoing)

async def main():
    async with asyncari.connect(ast_url, ast_app, ast_username,ast_password) as _client:
        global client
        client = _client
        await setup()
        await client.on_channel_event('StasisStart', on_start, client.taskgroup)
        # Run the WebSocket
        async for m in client:
            pprint(("** EVENT **", m,m._orig_msg))

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    anyio.run(main)

