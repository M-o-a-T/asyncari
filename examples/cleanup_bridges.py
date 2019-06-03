#!/usr/bin/python3

"""This program removes unused bridges.
"""

#
# Copyright (c) 2018, Matthias Urlichs
#
import asyncari
import anyio
from asks.errors import BadStatus

import logging
logger = logging.getLogger(__name__)

from pprint import pprint

import os
ast_host = os.getenv("AST_HOST", 'localhost')
ast_port = int(os.getenv("AST_ARI_PORT", 8088))
ast_url = os.getenv("AST_URL", 'http://%s:%d/'%(ast_host,ast_port))
ast_username = os.getenv("AST_USER", 'asterisk')
ast_password = os.getenv("AST_PASS", 'asterisk')
ast_app = os.getenv("AST_APP", 'hello')

async def clean_bridges(client):
    #
    # Find (or create) a holding bridge.
    #
    for b in await client.bridges.list():
        if b.channels:
            continue
        try:
            await b.destroy()
        except BadStatus as exc:
            print(b.id,exc)
        else:
            print(b.id,"… deleted")

async def main():
    async with asyncari.connect(ast_url, ast_app, ast_username,ast_password) as client:
        await clean_bridges(client)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    anyio.run(main)

