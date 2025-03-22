#!/usr/bin/python3

"""
We're implementing a door bell using Moat-KV, Asterisk, and hot glue.

You need
* a door bell. Moat-KV tells us when somebody presses it.
* a door station. We call it with SIP. It doesn't call us.
* any number of phones. We call them when the door bell rings, and they
  connect to the door when they call us.
* a door opener. We tell Moat-KV to trigger it.
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

    555! => &go(ext_bell,${EXTEN:3});

  (where '3' is the length of the prefix)


Start this script. It will register with Asterisk and listen to the
"bell" Moat-KV value (supposed to be a Boolean). When that is set to `True`
it'll connect to the door station, play a sound if set, then ring all
phones until one answers.

Conversely, if you call the door it'll connect your phone to it. If you
call the door while somebody else is talking, you'll join the existing call.

When the MoaT-KV "lock" value is True, calling the phones will be skipped.

If the DTMF code is entered, the door opener is triggered. All other DTMF
codes will be ignored. 
"""

#
# Copyright (c) 2013, Digium, Inc.
# Copyright (c) 2018-2023, Matthias Urlichs
#
import asyncari
import sys
import anyio
import logging
import asyncclick as click
from contextlib import asynccontextmanager
from functools import partial
from asyncari.state import ToplevelChannelState, HangupBridgeState, DTMFHandler, as_task, DialFailed, SyncPlay
from asyncari.model import ChannelExit, StateError
from moat.util import read_cfg, attrdict, combine_dict, P
from moat.kv.client import open_client
import uuid
from httpx import HTTPStatusError
import signal

from pprint import pprint

import os

CFG = attrdict(
    # Replacements via moat-kv
    replace = attrdict(
        data = attrdict(), # name > entry in "calls"
        tree = P("phone.s.redirect"),
    ),
    kv = attrdict(
        auth = "password name=asterisk password=you_guess",
        host = "127.0.0.1",
        init_timeout = 5,
        name = "Bell",
        port = 27586,
        ssl = False,
    ),
    door = attrdict(
        phone = "SIP/door",
        opener = P("home.ass.dyn.switch.door.cmd"),
        code = "0",
        audio = "/var/local/asterisk/hello"
    ),
    calls = attrdict(
#       std = attrdict(
#           bell = P("home.ass.dyn.binary_sensor.bell.state"),
#           audio = "/var/lib/asterisk/sounds/custom/door-std",
#           phones = [
#               'SIP/phone1',
#               'SIP/phone2',
#           ],
#           open = False,  # if set, auto-open the door
#       ),
    ),
    max_time = 300,  # 5min
    watchdog = attrdict(
        path = P("home.ass.ping.hass.reply"),
        timeout = 200,
    ),
    done = attrdict(  # list of signals that should stop calls
#       door = attrdict(
#           state = P("home.ass.dyn.switch.door.state"),
#           triggered = True,  # caused by the door opener?
#           kill = False,  # stop ongoing calls?
#       ),
    ),
    asterisk = attrdict(
        host = '127.0.0.1',
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
            for ch in b.channels:
                await ch.destroy()
            await b.destroy()

class DoorState(ToplevelChannelState, DTMFHandler):
    """
    State machine: The door.
    """
    def __init__(self, obj, channel, *, audio=False, opener=None):
        """
        @channel: chan to the door
        @audio: False: do nothing; True: play beep;
          None: play configured audio; string: play specified file
        """
        self.obj = obj
        self.audio = audio
        self.opener = opener
        super().__init__(channel)

    async def do_open(self):
        await anyio.sleep(self.opener)
        await self.obj.dkv.set(self.obj.cfg.door.opener, value=True, idem=False)
        await anyio.sleep(1)
        self.obj.block_done = False

    async def player(self):
        if self.opener is not False:
            # self.obj.block_done = True  # ??
            if isinstance(self.opener, (int,float)):
                self.obj.task.start_soon(self.do_open)

        f = self.audio
        if f is False:
            self.play_done.set()
        elif f is True:
            playback_id = str(uuid.uuid4())
            self.obj.log.warning("Play beep")
            p = await self.channel.playWithId(playbackId=playback_id,media="tone:beep;tonezone=de")
            await anyio.sleep(0.7)
            try:
                await p.stop()
            except HTTPStatusError:
                pass
        else:
            if f is None:
                f = self.obj.cfg.door.audio
            self.obj.log.warning("Play sound %s", f)
            await SyncPlay(self, media="sound:"+f)
            self.obj.log.warning("Play sound done")

        if self.opener is True:
            await self.obj.dkv.set(self.obj.cfg.door.opener, value=True, idem=False)
            await anyio.sleep(1)
            self.obj.block_done = False


    async def on_start(self):
        self.obj.task.start_soon(self.player)
        await super().on_start()

    async def on_DialResult(self, evt):
        await super().on_DialResult(evt)
        # answered OK
        self.obj.door.state = True

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
        code = self.obj.cfg.door.code
        if self.dtmf == code:
            self.obj.door.opened = True
            await self.obj.dkv.set(self.obj.cfg.door.opener, value=True, idem=False)
        else:
            # if the code is 223 and somebody keys in 2223, the door should open
            for i in range(len(self.dtmf)):
                if code.startswith(self.dtmf[i:]):
                    self.dtmf = self.dtmf[i:]
                    return
            # not a prefix if we get here
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
                if cs is not None and (self.n is None or i != self.n):
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
            await self.channel.answer()
            evt = anyio.Event()
            async def sub():
                playback_id = str(uuid.uuid4())
                p = await self.channel.playWithId(playbackId=playback_id,media="tone:beep;tonezone=de")
                await anyio.sleep(0.7)
                await p.stop()
                try:
                    await br.add(self.channel)
                except HTTPStatusError as exc:
                    self.obj.log.warning("SETUP ERROR %s", exc)
                evt.set()
            self.obj.task.start_soon(sub)

            await door_call(self.obj, audio=True)
            await evt.wait()
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
        obj.door.opened = False
        obj.door.called = set()
        if br is not None:
            for ch in br.bridge.channels:
                await ch.hang_up()
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

async def monitor_redirect(obj):
    old = {}

    obj.log.info("Update? on %s", obj.cfg.replace.tree)
    async with obj.dkv.watch(obj.cfg.replace.tree, min_depth=1, max_depth=1, fetch=True) as mon:
        async for msg in mon:
            if "path" not in msg:
                obj.log.debug("UPDATE START")
                continue
            op = msg.path[-1]
            if op not in obj.cfg.calls:
                obj.log.warning("UPDATE %s unknown", op)
                continue
            if op not in old:
                old[op] = obj.cfg.calls[op].copy()

            # clean up
            d = obj.cfg.calls[op]
            d.update(old[op])
            ex = set()
            for k in list(d.keys()):
                if k not in old[op]:
                    del d[k]

            val = msg.get("value", None)
            if val is None:
                obj.log.debug("UPDATE %s cleared", op)
            elif isinstance(val, dict):
                obj.log.debug("UPDATE %s %r", op, val)
                obj.cfg.calls[op].update(val)
            elif not isinstance(val, str):
                obj.log.debug("UPDATE %s %r", op, val)
            elif msg.value in obj.cfg.replace.data:
                obj.log.debug("UPDATE %s %r", op, val)
                obj.cfg.calls[op].update(obj.cfg.replace.data[val])
            else:
                obj.log.warning("UPDATE %s Unknown %r", op, msg)

async def monitor_phone_calls(obj):
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
    obj.door.opened = False
    obj.door.called = set()
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

async def _call(obj,n,dest,cid):
    """
    Call handler to a single phone.
    """
    with anyio.CancelScope() as sc:
        obj.calls.data[n] = sc
        try:
            ch = await obj.bridge.br.dial(endpoint=dest, State=partial(CalleeState,obj,n), callerId=cid)
            await ch.channel.wait_bridged()
            obj.log.info("Connected %d to %s",n,dest)
            obj.block_done = True  # don't hang up when the button is pressed
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

async def door_call(obj, audio=True, opener=None):
    """
    Call the door.

    Returns (and sets door.state.evt) as soon as the door is connected.
    """
    if obj.door.state is not None:
        await obj.door.state.wait()
        if obj.door.state is None:  # error
            raise RuntimeError("Door call failed")
        return

    obj.door.state = e = anyio.Event()
    async with with_bridge(obj) as br:
        try:
            r = await br.dial(endpoint=obj.cfg.door.phone, State=partial(DoorState,obj,audio=audio,opener=opener))
            return r
        except BaseException:
            obj.door.state = None
            raise
        finally:
            e.set()


async def call_phones(obj,name,c):
    """
    Call a number of phones (including zero).
    """
    if not c['phones']:
        obj.log.info("from door: %s: No phones", name)
        return
    obj.log.info("from door: %s: Calling phones", name)
    cid = c.get("caller",{})
    cid = attrdict(name=cid.get("name","?"), number=cid.get("nr",""))
    cid = f"{cid.name} <{cid.number}>"
    for dest in c['phones']:
        if dest in obj.door.called:
            obj.log.info("from door: %s: %s: already called", name, dest)
            continue
        obj.log.info("from door: %s: %s: calling", name, dest)
        n = len(obj.calls.data)
        obj.calls.data.append(None)
        obj.bridge.br._tg.start_soon(_call,obj,n,dest,cid)

    with anyio.move_on_after(obj.cfg.max_time):
        await obj.calls.evt.wait()
    for cs in obj.calls.data:
        if cs is not None:
            cs.cancel()

async def monitor_watchdog(obj):
    wd = obj.cfg.watchdog
    evt = anyio.Event()

    async def mon():
        nonlocal evt
        async with obj.dkv.watch(wd.path, max_depth=0, fetch=False) as mon:
            async for evt in mon:
                evt.set()
                evt = anyio.Event()
            raise RuntimeError("Watchdog ended")

    async with anyio.create_task_group() as tg:
        tg.start(mon)
        while True:
            with anyio.fail_after(wd.timeout):
                await evt.wait()



async def call_from_door(obj,name,c):
    """
    Somebody pressed the button on the door.

    We connect to the door, then start to call phones.
    """
    if obj.door.state is not None:
        obj.log.info("from door: %s: already connected",name)
        await call_phones(obj,name,c)
        return

    try:
        obj.log.info("from door: %s: Calling door",name)
        async with with_bridge(obj) as br:
            try:
                opener = c["open"]
            except KeyError:
                opener = None
            else:
                if opener is True:
                    opener = obj.cfg.door.opener

            try:
                fn = c["audio"]
            except KeyError:
                fn = obj.cfg.door.audio

            try:
                r = await door_call(obj, audio=fn, opener=opener)
            except ChannelExit as exc:
                obj.log.exception("NOT CALLED %r",exc)
                return
            obj.log.info("from door: %s: Connected to door (%r)", name, br.bridge.channels)
            obj.calls.data = []
            obj.calls.evt = anyio.Event()

            await call_phones(obj,name,c)
            if r.channel.bridge is None:
                chs = br.bridge.channels.copy()
                pp = []
                for ch in chs:
                    playback_id = str(uuid.uuid4())
                    pp.append(await ch.playWithId(playbackId=playback_id,media="tone:stutter;tonezone=de"))
                try:
                    await r.channel.wait_bridged()
                except StateError as exc:
                    obj.log.warning("from door: no channel (%r). Disconnecting.", exc)
                    return
                
                for p in pp:
                    await p.stop()

            if sum(ch.state == 'Up' for ch in br.bridge.channels) >= 2:
                # call established
                obj.log.info("from door: OK. Waiting for hangup")
                await br

            else:
                obj.log.info("from door: not OK. Disconnecting.")
                # TODO Play a sound or something?

    finally:
        obj.log.info("from door: Terminating door")
        obj.door.state = None
        
async def monitor_call(obj,name,c):
    """
    Monitor call buttons
    """
    async with obj.dkv.watch(c['bell'], max_depth=0, fetch=False) as mon:
        async for evt in mon:
            if not evt.get("value",False):
                continue
            if obj.door.state is not None:
                continue
            obj.ari.taskgroup.start_soon(call_from_door, obj,name,c)

async def monitor_done(obj,name,c):
    """
    Monitor action result signals
    """
    try:
      obj.log.info("Watch? %s",c['state'])
      async with obj.dkv.watch(c['state'], max_depth=0, fetch=False) as mon:
        async for evt in mon:
            if obj.block_done:
                obj.log.info("Watch %s BLOCK %s",c['state'],evt)
                continue
            if not evt.get("value",False):
                obj.log.info("Watch %s NOVAL %s",c['state'],evt)
                continue
            if obj.door.state is None:
                obj.log.info("Watch %s NONE %s",c['state'],evt)
                continue
            if c.get('triggered',False) and obj.door.opened:
                obj.log.info("Watch %s TRIG %s",c['state'],evt)
                obj.door.opened = False
                continue
            if c.get('kill',False):
                obj.log.info("Watch %s KILL %s",c['state'],evt)
                obj.bridge.br.hang_up()
            else:
                for cs in obj.calls.data:
                    if cs is not None:
                        obj.log.info("Watch %s CANCEL %s",c['state'],evt)
                        cs.cancel()
                    else:
                        obj.log.info("Watch %s XCANCEL %s",c['state'],evt)
                else:
                    obj.log.info("Watch %s NOCANCEL %s",c['state'],evt)
    except Exception as exc:
        obj.log.exception("Owch %r %r",name,c)

async def monitor_sig(obj):
    with anyio.open_signal_receiver(signal.SIGUSR1) as mon:
        async for _ in mon:
            pprint(obj, sys.stderr)

@click.command()
@click.option("-v", "--verbose", count=True, help="Be more verbose. Can be used multiple times.")
@click.option("-q", "--quiet", count=True, help="Be less verbose. Opposite of '--verbose'.")
@click.option("-c", "--cfg", type=click.Path("r"), default=None, help="Configuration file (YAML).")
@click.pass_context
async def main(ctx, verbose,quiet,cfg):
    verbose = verbose-quiet+1
    logging.basicConfig(level=[logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG][min(verbose,3)])
    ctx.obj = obj = attrdict()
    obj.redir = dict()
    cf = read_cfg("bell", cfg)
    cf = CFG if cf is None else combine_dict(cf, CFG, cls=attrdict)
    ast = cf.asterisk
    ast.url = f'http://{ast.host}:{ast.port}/'
    obj.cfg = cf
    obj.log = logging.getLogger("bell")

    obj.ari = await ctx.with_async_resource(asyncari.connect(ast.url, ast.app, ast.username,ast.password))

    obj.dkv = await ctx.with_async_resource(open_client(conn=cf.kv))
    obj.block_done = False

    await bridge_cleanup(obj)

    async with anyio.create_task_group() as obj.task:
        for name,c in obj.cfg.calls.items():
            obj.task.start_soon(monitor_call, obj,name,c)
        for name,c in obj.cfg.done.items():
            obj.task.start_soon(monitor_done, obj,name,c)
        # client.taskgroup.start_soon(monitor_calls, client)
        obj.task.start_soon(monitor_redirect, obj)
        obj.task.start_soon(monitor_sig, obj)
        if obj.cfg.watchdog.timeout:
            obj.task.start_soon(monitor_watchdog, obj)
        await monitor_phone_calls(obj)

if __name__ == "__main__":
    try:
        main(_anyio_backend="trio")
    except KeyboardInterrupt:
        pass

