#!/usr/bin/python3

"""Short example of how to use bridge objects.

This example will create a holding bridge (if one doesn't already exist). Any
channels that enter Stasis is placed into the bridge. Whenever a channel
enters the bridge, a tone is played to the bridge.
"""

#
# Copyright (c) 2013, Digium, Inc.
#

import anyio
import asyncari
import logging

import os
ast_host = os.getenv("AST_HOST", 'localhost')
ast_port = int(os.getenv("AST_ARI_PORT", 8088))
ast_url = os.getenv("AST_URL", 'http://%s:%d/'%(ast_host,ast_port))
ast_username = os.getenv("AST_USER", 'asterisk')
ast_password = os.getenv("AST_PASS", 'asterisk')
ast_app = os.getenv("AST_APP", 'hello')

bridge = None

async def setup(client):
    global bridge
    #
    # Find (or create) a holding bridge.
    #
    bridges = [b for b in (await client.bridges.list()) if
            b.json['bridge_type'] == 'holding']
    if bridges:
        bridge = bridges[0]
        print("Using bridge %s" % bridge.id)
    else:
        bridge = await client.bridges.create(type='holding')
        print("Created bridge %s" % bridge.id)

    bridge.on_event('ChannelEnteredBridge', on_enter)

async def on_enter(bridge, ev):
    """Callback for bridge enter events.

    When channels enter the bridge, play tones to the whole bridge.

    :param bridge: Bridge entering the channel.
    :param ev: Event.
    """
    # ignore announcer channels - see ASTERISK-22744
    if ev['channel']['name'].startswith('Announcer/'):
        return
    await bridge.play(media="sound:ascending-2tone")




async def stasis_start_cb(objs, ev):
    """Callback for StasisStart events.

    For new channels, answer and put them in the holding bridge.

    :param channel: Channel that entered Stasis
    :param ev: Event
    """
    channel = objs['channel']
    await channel.answer()
    await bridge.addChannel(channel=channel.id)


async def main():
    async with asyncari.connect(ast_url, ast_app, ast_username,ast_password) as client:
        await setup(client)
        client.on_channel_event('StasisStart', stasis_start_cb)
        # Run the WebSocket
        await anyio.sleep_forever()

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    anyio.run(main)

