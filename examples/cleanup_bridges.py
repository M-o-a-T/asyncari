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

from aiohttp.web_exceptions import HTTPError, HTTPNotFound, HTTPBadRequest
from pprint import pprint

import os
ast_url = os.getenv("AST_URL", 'http://localhost:8088/')
ast_username = os.getenv("AST_USER", 'asterisk')
ast_password = os.getenv("AST_PASS", 'asterisk')
ast_app = os.getenv("AST_APP", 'hello')
ast_outgoing = os.getenv("AST_OUTGOING", 'SIP/blink')

async def clean_bridges(client):
    #
    # Find (or create) a holding bridge.
    #
    for b in await client.bridges.list():
        if b.channels:
            continue
        await b.destroy()

async def main():
    async with trio_ari.connect(ast_url, ast_app, ast_username,ast_password) as client:
        await clean_bridges(client)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    trio_asyncio.run(main)

