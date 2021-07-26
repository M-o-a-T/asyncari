#!/usr/bin/python3

"""
We're implementing a door bell using DistKV, Asterisk, and hot glue.

You need
* a door bell. DistKV tells us when somebody presses it.
* a door station. We call it with SIP. It doesn't call us.
* any number of phones. We call them when the door bell rings, and they
  connect to the door when they call us.
* a door opener. We tell DistKV to trigger it.
* an Asterisk server with ARI enabled.
* this macro+context in `extensions.ael`, assuming you keep this script's
  default program name of "bell":

    macro py(app,typ,ext) {
        Stasis(${app},${typ},${ext});
        Hangup();
        return;
    }
    context ext_bell {
        s => &py(bell,test,s);
        i => &py(bell,test,${INVALID_EXTEN});
    }

* this dialplan entry, which calls the door:

    123! => &go(ext_bell,${EXTEN:3});

  (where '3' is the length of your prefix)


Start this script. It will register with Asterisk and listen to the
"bell" DistKV value (supposed to be a Boolean). When that is set to `True`
it'll connect to the door station, then ring all phones until one answers.

Conversely, if you call the door it'll connect your phone to it. If you
call the door while somebody else is talking, you'll get connected.
"""

#
# Copyright (c) 2013, Digium, Inc.
# Copyright (c) 2018-2021, Matthias Urlichs
#
import asyncari
import anyio
import logging
import asyncclick as click
from contextlib import asynccontextmanager
from functools import partial
from asyncari.state import ToplevelChannelState, HangupBridgeState, DTMFHandler, as_task, DialFailed
from asyncari.model import ChannelExit
from distkv.util import read_cfg, attrdict, combine_dict, P
from distkv.client import open_client

from pprint import pprint

import os

CFG = attrdict(
    distkv = attrdict(
        auth = "password name=ast password=erisk",
        host = "127.0.0.1",
        init_timeout = 5,
        name = "Bell",
        port = 27586,
        ssl = False,
    ),
    door = "SIP/door",
    phones = [
        'SIP/phone1',
        'SIP/phone2',
    ],
    max_time = 300,  # 5min
    at = attrdict(  # distkv paths
        bell = P("home.ass.dyn.binary_sensor.bell.state"),
        door = P("home.ass.dyn.switch.door.cmd"),
    ),
    code = "0",
    asterisk = attrdict(
        host = 'localhost',
        port = 8088,
        username = 'asterisk',
        password = 'asterisk',
        app = 'bell',

        bridge_name = "Bell"
    ),
)

# This demonstrates incoming DTMF recognition on both legs of a call

async def bridge_cleanup(obj):
    for b in await obj.ari.bridges.list():
        if b.name == obj.cfg.asterisk.bridge_name:
            for c in b.channels:
                await c.destroy()
            await b.destroy()

class DoorState(ToplevelChannelState, DTMFHandler):
    """
    State machine: The door.
    """
    def __init__(self, obj, channel):
        self.obj = obj
        super().__init__(channel)

    async def on_DialResult(self, evt):
        super().on_DialResult(evt)
        # answered OK
        self.obj.door.state = True
        self.obj.door.evt.set()

    async def on_dtmf(self,evt):
        print("*DTMF*EXT*",evt.digit)

class _CallState(ToplevelChannelState, DTMFHandler):
    """
    State machine: phone.
    """
    def __init__(self, obj, n, channel):
        self.obj = obj
        self.n = n
        self.dtmf = ""
        super().__init__(channel)

    async def on_dtmf(self,evt):
        print("*DTMF*INT*",evt.digit)
        self.dtmf += evt.digit
        code = self.obj.cfg.code
        if self.dtmf == code:
            await self.obj.dkv.set(self.obj.cfg.at.door, value=True, idem=False)
        elif code.startswith(self.dtmf):
            return
        self.dtmf = ""

    async def on_DialResult(self, evt):
        try:
            await super().on_DialResult(evt)
        except DialFailed:
            if self.n is not None:
                self.obj.calls.data[self.n] = None
            if any(self.obj.calls.data):
                return
            # Ugh, nobody home
            raise

        else:
            if self.obj.calls.data is None:
                return
            if self.n is not None:
                self.obj.calls.data[self.n] = None
            for i,cs in enumerate(self.obj.calls.data):
                if i != n and cs is not None:
                    cs.cancel()


class CalleeState(_CallState, DTMFHandler):
    """
    State machine: the door calls somebody.
    """
    pass

class CallerState(_CallState):
    """
    State machine: somebody calls the door.
    """
    def __init__(self, obj, channel):
        super().__init__(obj, None, channel)

    @as_task
    async def on_start(self):
        await super().on_start()

        async with with_bridge(self.obj) as br:
            if self.obj.door.state is not None:
                await self.channel.answer()
            await br.add(self.channel)
            await door_call(self.obj)
            if self.obj.door.state is not None:
                await self.channel.answer()
                await self.channel.wait_not_bridged()


async def _run_bridge(obj, *, task_status):
    br = None
    try:
        obj.log.info("Setting up bridge")
        with anyio.CancelScope() as sc:
            async with HangupBridgeState.new(obj.ari, name=obj.cfg.asterisk.bridge_name) as br:
                obj.bridge.br = br
                obj.bridge.scope = sc
                task_status.started()
                with anyio.move_on_after(obj.cfg.max_time):
                    await br  # waits for end
    except BaseException as exc:
        obj.log.exception("Bridge? %r",exc)
        raise

    finally:
        obj.log.info("Stopping bridge")
        obj.bridge.br = None
        obj.bridge.scope = None
        obj.bridge.cnt = 0
        obj.door.state = None
        if br is not None:
            await br.teardown()
        obj.log.info("Stopped bridge")


@asynccontextmanager
async def with_bridge(obj):
    if obj.bridge.br is None:
        async with obj.bridge.lock:
            if obj.bridge.br is None:
                if obj.bridge.cnt > 0:
                    raise RuntimeError("Bridge count %d" % (obj.bridge.cnt,))
                await obj.task.start(_run_bridge, obj)
                if obj.bridge.br is None:
                    raise RuntimeError("No bridge")

    obj.bridge.cnt += 1
    try:
        yield obj.bridge.br
    finally:
        obj.bridge.cnt -= 1
        if obj.bridge.cnt == 0 and obj.bridge.scope is not None:
            obj.bridge.scope.cancel()


async def monitor_calls(obj):
    """Wait for StasisStart events, indicating that a phone calls the
    bridge.
    """

    obj.bridge = attrdict()
    obj.bridge.task = None
    obj.bridge.br = None
    obj.bridge.scope = None
    obj.bridge.lock = anyio.Lock()
    obj.bridge.cnt = 0
    obj.door = attrdict()
    obj.door.state = None
    obj.calls = attrdict()
    obj.calls.evt = None
    obj.calls.data = None

    # Answer and put in the holding bridge
    async with obj.ari.on_channel_event('StasisStart') as listener:
        async for objs, event in listener:
            if event['args'][0] == 'dialed':
                continue
            incoming = objs['channel']
            cs = CallerState(obj, incoming)
            await cs.start_task()

async def _call(br,obj,n,dest):
    """
    Call handler to a single phone.
    """
    with anyio.CancelScope() as sc:
        obj.calls.data[n] = sc
        try:
            ch = await br.dial(endpoint=dest, State=partial(CalleeState,obj,n))
            await ch.channel.wait_bridged()
            obj.log.info("Connected %d to %s",n,dest)
            obj.calls.evt.set()
            for d in obj.calls.data:
                if d is not None:
                    d.cancel()
        except ChannelExit:
            obj.calls.data[n] = None
            if not any(obj.calls.data):
                # all calls failed
                obj.calls.evt.set()
        finally:
            obj.calls.data[n] = None

async def door_call(obj):
    """
    Call the door.

    Returns as soon as the door is connected.
    """
    if obj.door.state is not None:
        await obj.door.state.wait()
        if obj.door.state is None:  # error
            raise RuntimeError("Door call failed")
        return

    obj.door.state = e = anyio.Event()
    async with with_bridge(obj) as br:
        try:
            await br.dial(endpoint=obj.cfg.door, State=partial(DoorState,obj))
        except BaseException:
            obj.door.state = None
            raise
        finally:
            e.set()


async def call_from_door(obj):
    """
    Somebody pressed the button on the door.

    We connect to the door, then start to call phones.
    """
    if obj.door.state is not None:
        obj.log.info("from door: already connected")
        return

    try:
        obj.log.info("from door: Calling door")
        async with with_bridge(obj) as br:
            try:
                await door_call(obj)
            except ChannelExit as exc:
                obj.log.exception("NOT CALLED %r",exc)
                return
            obj.log.info("from door: Connected to door")
            obj.calls.data = []
            obj.calls.evt = anyio.Event()
            obj.log.info("from door: Calling phones")
            for n,dest in enumerate(obj.cfg.phones):
                obj.calls.data.append(None)
                br._tg.start_soon(_call,br,obj,n,dest)

            with anyio.move_on_after(obj.cfg.max_time):
                await obj.calls.evt.wait()
            for cs in obj.calls.data:
                if cs is not None:
                    cs.cancel()

            if sum(c.state == 'Up' for c in br.bridge.channels) >= 2:
                # call established
                obj.log.info("from door: OK. Waiting for hangup")
                await br

            else:
                obj.log.info("from door: not OK. Disconnecting.")
                # TODO Play a sound or something?

    finally:
        obj.log.info("from door: Terminating door")
        obj.door.state = None
        
async def monitor_distkv(obj):
    async with obj.dkv.watch(obj.cfg.at.bell, max_depth=0, fetch=False) as mon:
        async for evt in mon:
            if not evt.get("value",False):
                continue
            if obj.door.state is not None:
                continue
            obj.ari.taskgroup.start_soon(call_from_door, obj)

@click.command()
@click.option("-v", "--verbose", count=True, help="Be more verbose. Can be used multiple times.")
@click.option("-q", "--quiet", count=True, help="Be less verbose. Opposite of '--verbose'.")
@click.option("-c", "--cfg", type=click.Path("r"), default=None, help="Configuration file (YAML).")
@click.pass_context
async def main(ctx, verbose,quiet,cfg):
    verbose = verbose-quiet+1
    logging.basicConfig(level=[logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG][min(verbose,3)])
    ctx.obj = obj = attrdict()
    cf = read_cfg("bell", cfg)
    cf = CFG if cf is None else combine_dict(cf, CFG, cls=attrdict)
    ast = cf.asterisk
    ast.url = f'http://{ast.host}:{ast.port}/'
    obj.cfg = cf
    obj.log = logging.getLogger("bell")

    obj.ari = await ctx.with_async_resource(asyncari.connect(ast.url, ast.app, ast.username,ast.password))
    obj.dkv = await ctx.with_async_resource(open_client(connect=cf.distkv))
    await bridge_cleanup(obj)

    async with anyio.create_task_group() as obj.task:
        obj.task.start_soon(monitor_distkv, obj)
        # client.taskgroup.start_soon(monitor_calls, client)
        await monitor_calls(obj)

if __name__ == "__main__":
    try:
        main(_anyio_backend="trio")
    except KeyboardInterrupt:
        pass

