#!/usr/bin/python3

"""Example demonstrating using the returned object from an API call.

This app plays demo-contrats on any channel sent to Stasis(hello). DTMF keys
are used to control the playback.
"""

#
# Copyright (c) 2013, Digium, Inc.
#

import anyio
import asyncari
import sys
import logging

import os
ast_host = os.getenv("AST_HOST", 'localhost')
ast_port = int(os.getenv("AST_ARI_PORT", 8088))
ast_url = os.getenv("AST_URL", 'http://%s:%d/'%(ast_host,ast_port))
ast_username = os.getenv("AST_USER", 'asterisk')
ast_password = os.getenv("AST_PASS", 'asterisk')
ast_app = os.getenv("AST_APP", 'hello')

async def on_start(objs, event):
    """Callback for StasisStart events.

    On new channels, answer, play demo-congrats, and register a DTMF listener.

    :param channel: Channel DTMF was received from.
    :param event: Event.
    """
    channel = objs['channel']
    await channel.answer()
    playback = await channel.play(media='sound:demo-congrats')

    async def on_dtmf(channel, event):
        """Callback for DTMF events.

        DTMF events control the playback operation.

        :param channel: Channel DTMF was received on.
        :param event: Event.
        """
        # Since the callback was registered to a specific channel, we can
        #  control the playback object we already have in scope.
        # TODO: if paused: unpause before doing anything else
        digit = event['digit']
        if digit == '5':
            await playback.control(operation='pause')
        elif digit == '8':
            await playback.control(operation='unpause')
        elif digit == '4':
            await playback.control(operation='reverse')
        elif digit == '6':
            await playback.control(operation='forward')
        elif digit == '2':
            await playback.control(operation='restart')
        elif digit == '#':
            await playback.stop()
            await channel.continueInDialplan()
        else:
            print >> sys.stderr, "Unknown DTMF %s" % digit

    channel.on_event('ChannelDtmfReceived', on_dtmf)

async def main():
    async with asyncari.connect(ast_url, ast_app, ast_username,ast_password) as client_:
        global client
        client = client_
        client.on_channel_event('StasisStart', on_start)
        # Run the WebSocket
        async for m in client:
            print("** EVENT **", m)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    anyio.run(main)

