#
# Copyright (c) 2018 Matthias Urlichs
#

"""Asyncified ARI client library.
"""

import re
import os
import json
import urllib
import anyio
from asyncswagger11.client import SwaggerClient
import time
import inspect
import sys
from pprint import pformat

from wsproto.events import CloseConnection, TextMessage
from .model import CLASS_MAP

from functools import partial

from .model import Repository
from .model import Channel, Bridge, Playback, LiveRecording, StoredRecording, Endpoint, DeviceState, Sound

import logging
log = logging.getLogger(__name__)

__all__ = ["Client"]

class _EventHandler(object):
    """Class to allow events to be unsubscribed.
    """

    def __init__(self, client, event_type, mangler=None, filter=None):
        self.client = client
        self.event_type = event_type
        self.mangler = mangler
        if filter is None:
            def filter(evt):
                return True
        self.filter = filter

    async def __call__(self, msg):
        if not self.filter(msg):
            return
        await self.q.put(msg)

    def open(self):
        self.q = anyio.create_queue(10)
        log.debug("ADD %s",self.event_type)
        self.client.event_listeners.setdefault(self.event_type, list()).append(self)

    def close(self):
        log.debug("DEL %s",self.event_type)
        self.client.event_listeners[self.event_type].remove(self)

    async def __aenter__(self):
        self.open()
        return self

    async def __aexit__(self, *tb):
        self.close()

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            res = await self.q.get()
            if self.mangler:
                res = self.mangler(res)
                if res is None:
                    continue
            return res

class Client:
    """Async ARI Client object.

    :param taskgroup: the AnyIO taskgroup to run our task(s) in.
    :param apps: the Stasis app(s) to register for.
    :param base_url: Base URL for accessing Asterisk.
    :param http_client: HTTP client interface.
    """

    def __init__(self, taskgroup, base_url, apps, http_client):
        self.taskgroup = taskgroup
        self._apps = apps
        url = urllib.parse.urljoin(base_url, "ari/api-docs/resources.json")
        self.swagger = SwaggerClient(http_client=http_client, url=url)
        self.class_map = CLASS_MAP.copy()
        tm = time.time()
        self._id_name = "ARI.%x.%x%03x" % (os.getpid(),int(tm),int(tm*0x1000)&0xFFF)
        self._id_seq = 0
        self._reader = None  # Event reader

    def __repr__(self):
        return "<%s:%s>" % (self.__class__.__name__, self._id_name)

    def generate_id(self, typ=""):
        self._id_seq += 1
        return "%s.%s%d" % (self._id_name, typ, self._id_seq)
        
    def is_my_id(self, id):
        if id == self._id_name:
            return True
        return id.startswith(self._id_name+'.')

    async def __aenter__(self):
        await self._init()
        await self.taskgroup.spawn(self._run)
        return self

    async def __aexit__(self, *tb):
        async with anyio.fail_after(1, shield=True) as scope:
            await self.close()

    async def new_channel(self, State, endpoint, **kw):
        """Create a new channel. Keywords 'timeout' 'variables'
        'originator' 'formats' are as in ARI.channels.originateWithID().

        :param State: The :class:`OutgoingState` factory to use.
        Called with the new channel.

        Returns: the state of the channel. Note that this state
        will have to wait for the initial ``StasisBegin`` event.
        """
        id = self.client.generate_id()
        chan = Channel(self, id=id)
        ch = await self.channels.originateWithId(endpoint=endpoint, app=self._app, **kw)
        return State(ch)

    def __enter__(self):
        raise RuntimeError("You need to call 'async with …'.")

    def __exit__(self, *tb):
        raise RuntimeError("You need to call 'async with …'.")

    def __iter__(self):
        raise RuntimeError("You need to call 'async for …'.")

    def __aiter__(self):
        if self._reader is None:
            self._reader = _EventHandler(self,'*')
            self._reader.open()
        return self._reader

    def on_start_of(self, endpoint):
        """
        Iterator for StasisStart on a particular sub-endpoint.

        Returns an async iterator that yields (channel,start_event) tuples.
        """
        return self.on_channel_event("StasisStart",
                filter=lambda evt: evt.args[0] == endpoint)

    @property
    def app(self):
        return self._app

    async def _run(self, evt: anyio.abc.Event = None):
        """Connect to the WebSocket and begin processing messages.

        This method will block until all messages have been received from the
        WebSocket, or until this client has been closed.

        :param apps: Application (or list of applications) to connect for
        :type  apps: str or list of str

        This is a coroutine. Don't call it directly, it's autostarted by
        the context manager.
        """
        ws = None
        apps = self._apps
        if isinstance(apps, list):
            self._app = apps[0]
            apps = ','.join(apps)
        else:
            self._app = apps.split(',',1)[0]

        try:
            ws = await self.swagger.events.eventWebsocket(app=apps)
            self.websockets.add(ws)

            # For tests
            if evt is not None:
                await evt.set()

            await self.__run(ws)

        finally:
            if ws is not None:
                self.websockets.remove(ws)
                async with anyio.open_cancel_scope(shield=True):
                    await ws.close()
            del self._app

    async def _check_runtime(self, recv, evt: anyio.abc.Event = None):
        """This gets streamed a message when processing begins, and `None`
        when it ends. Repeat.
        """
        if evt is not None:
            await evt.set()
        while True:
            msg = await recv.get()
            if msg is False:
                return
            assert msg is not None

            try:
                async with anyio.fail_after(0.2):
                    msg = await recv.get()
                    if msg is False:
                        return
                    assert msg is None
            except TimeoutError:
                log.error("Processing delayed: %s", msg)
                t = await anyio.current_time()
                # don't hard-fail that fast when debugging
                async with anyio.fail_after(1 if 'pdb' not in sys.modules else 99):
                    msg = await recv.get()
                    if msg is False:
                        return
                    assert msg is None
                    pass  # processing delayed, you have a problem
                log.error("Processing recovered after %.2f sec", (await anyio.current_time())-t)

    async def __run(self, ws):
        """Drains all messages from a WebSocket, sending them to the client's
        listeners.

        :param ws: WebSocket to drain.
        """
        q = anyio.create_queue(0)
        await self.taskgroup.spawn(self._check_runtime, q)

        async for msg in ws:
            if isinstance(msg, CloseConnection):
                break
            elif not isinstance(msg, TextMessage):
                log.warning("Unknown JSON message type: %s", repr(msg))
                continue # ignore
            msg_json = json.loads(msg.data)
            if not isinstance(msg_json, dict) or 'type' not in msg_json:
                log.error("Invalid event: %s", msg)
                continue
            try:
                await q.put(msg_json)
                await self.process_ws(msg_json)
            finally:
                await q.put(None)
        await q.put(False)

    async def _init(self, RepositoryFactory=Repository):
        await self.swagger.init()
        # Extract models out of the events resource
        events = [api['api_declaration']
                  for api in self.swagger.api_docs['apis']
                  if api['name'] == 'events']
        if events:
            self.event_models = events[0]['models']
        else:
            self.event_models = {}

        self.repositories = {
            name: Repository(self, name, api)
            for (name, api) in self.swagger.resources.items()}
        self.websockets = set()
        self.event_listeners = {}

    def __getattr__(self, item):
        """Exposes repositories as fields of the client.

        :param item: Field name
        """
        repo = self.get_repo(item)
        if not repo:
            raise AttributeError(
                "'%r' object has no attribute '%s'" % (self, item))
        return repo

    async def close(self):
        """Close this ARI client.

        This method will close any currently open WebSockets, and close the
        underlying Swaggerclient.
        """
        for ws in list(self.websockets): # changes during processing
            await ws.close()
        await self.swagger.close()

    def get_repo(self, name):
        """Get a specific repo by name.

        :param name: Name of the repo to get
        :return: Repository, or None if not found.
        :rtype:  asyncari.model.Repository
        """
        return self.repositories.get(name)

    async def process_ws(self, msg):
        """Process one incoming websocket message.
        """
        msg = EventMessage(self, msg)

        # First, call traditional listeners
        log.debug("DISP ***** Dispatch:%s\n%s", msg, pformat(vars(msg)))
        listeners = list(self.event_listeners.get(msg['type'], [])) \
                    + list(self.event_listeners.get('*', []))
        for listener in listeners:
            cb = await listener(msg)

        # Next, dispatch the event to the objects in the message
        await msg._send_event()

    def on_event(self, event_type, mangler=None, filter=None):
        """Listener for events with given type.

        :param event_type: String name of the event to register for.

        Usage::

            async with client.on_object_event("StasisStart") as listener:
                async for objs, event in listener:
                    await client.spawn(handle_new_client, objs, event)
        """
        return _EventHandler(self, event_type, mangler=mangler, filter=filter)

    def on_object_event(self, event_type, factory_fn, model_id, filter=None):
        """Listener for events with the given type. Event fields of
        the given model_id type are converted to objects.

        If multiple fields of the event have the type ``model_id``, a dict is
        passed mapping the field name to the model object.

        :param event_type: String name of the event to register for.
        :param event_cb: Callback function
        :type  event_cb: (Obj, dict) -> None or (dict[str, Obj], dict) ->
        :param factory_fn: Function for creating Obj from JSON
        :param model_id: String id for Obj from Swagger models.

        Usage::

            async with client.on_object_event("StasisStart", Channel,"Channel") as listener:
                async for objs, event in listener:
                    await client.spawn(handle_new_client, objs, event)
        """
        # Find the associated model from the Swagger declaration
        event_model = self.event_models.get(event_type)
        if not event_model:
            raise ValueError("Cannot find event model '%s'" % event_type)

        # Extract the fields that are of the expected type
        obj_fields = [k for (k, v) in event_model['properties'].items()
                      if v['type'] == model_id]
        if not obj_fields:
            raise ValueError("Event model '%s' has no fields of type %s"
                             % (event_type, model_id))

        def extract_objects(event):
            """Extract objects of a given type from an event.

            :param event: Event
            :param args: Arguments to pass to the event callback
            :param kwargs: Keyword arguments to pass to the event
                                      callback
            """
            # Extract the fields which are of the expected type
            obj = {obj_field: factory_fn(self, json=event[obj_field])
                   for obj_field in obj_fields
                   if event._get(obj_field)}
            # If there's only one field in the schema, just pass that along
            if len(obj_fields) == 1:
                if obj:
                    vals = list(obj.values())
                    obj = vals[0]
                else:
                    obj = None
            return (obj, event)

        return self.on_event(event_type, mangler=extract_objects, filter=filter)

    def on_channel_event(self, event_type, filter=None):
        """Listener for Channel related events

        :param event_type: Name of the event to register for.

        Usage::

            async with client.on_channel_event("StasisStart") as listener:
                async for objs, event in listener:
                    await client.spawn(handle_new_client, objs, event)
        """
        return self.on_object_event(event_type, Channel, 'Channel', filter=filter)

    def on_bridge_event(self, event_type, filter=None):
        """Listener for Bridge related events

        :param event_type: Name of the event to register for.
        """
        return self.on_object_event(event_type, Bridge, 'Bridge', filter=filter)

    def on_playback_event(self, event_type, filter=None):
        """Listener for Playback related events

        :param event_type: Name of the event to register for.
        """
        return self.on_object_event(event_type, Playback, 'Playback', filter=filter)

    def on_live_recording_event(self, event_type, filter=None):
        """Listener for LiveRecording related events

        :param event_type: Name of the event to register for.
        """
        return self.on_object_event(event_type, LiveRecording, 'LiveRecording', filter=filter)

    def on_stored_recording_event(self, event_type, filter=None):
        """Listener for StoredRecording related events

        :param event_type: Name of the event to register for.
        """
        return self.on_object_event(event_type, StoredRecording, 'StoredRecording', filter=filter)

    def on_endpoint_event(self, event_type, filter=None):
        """Listener for Endpoint related events

        :param event_type: Name of the event to register for.
        """
        return self.on_object_event(event_type, Endpoint, 'Endpoint', filter=filter)

    def on_device_state_event(self, event_type, filter=None):
        """Listener for DeviceState related events

        :param event_type: Name of the event to register for.
        """
        return self.on_object_event(event_type, DeviceState, 'DeviceState', filter=filter)

    def on_sound_event(self, event_type, filter=None):
        """Listener for Sound related events

        :param event_type: Name of the event to register for.
        """
        return self.on_object_event(event_type, Sound, 'Sound', filter=filter)

class EventMessage:
    """This class encapsulates an event.
    All elements with known types are converted to objects,
    if a class for them is registered.

    Note::
        The "Dial" event is converted to "DialStart", "DialState" or
        "DialResult" depending on whether ``dialstatus`` is empty or not.
    """
    def __init__(self, client, msg):
        self._client = client
        self._orig_msg = msg

        event_type = msg['type']
        event_model = client.event_models.get(event_type)
        if not event_model:
            log.warn("Cannot find event model '%s'" % event_type)
            return
        event_model = event_model.get('properties', {})

        for k, v in msg.items():
            setattr(self, k, v)

            m = event_model.get(k)
            if m is None:
                continue
            t = m['type']
            is_list = False
            m = re.match(r'''List\[(.*)\]''', t)
            if m:
                t = m.group(1)
                is_list = True
            factory = client.class_map.get(t)
            if factory is None:
                continue
            if is_list:
                v = [factory(client, json=obj) for obj in v]
            else:
                v = factory(client, json=v)

            setattr(self, k, v)

        if self.type == "Dial":
            if self.dialstatus == "":
                self.type = "DialStart"
            elif self.dialstatus in {"PROGRESS", "RINGING"}:
                self.type = "DialState"
            else:
                self.type = "DialResult"

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, self.type)

    async def _send_event(self):
        for k in self._orig_msg.keys():
            v = getattr(self, k)
            do_ev = getattr(v, 'do_event', None)
            if do_ev is not None:
                await do_ev(self)

    def __getitem__(self, k):
        return self._orig_msg.__getitem__(k)

    def _get(self, k, v=None):
        return self._orig_msg.get(k, v)


