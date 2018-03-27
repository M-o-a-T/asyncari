#
# Copyright (c) 2018 Matthias Urlichs
#

"""Trio-ified ARI client library.
"""

import re
import json
import urllib
import aiohttp
import trio_asyncio
import trio
import aioswagger11
import inspect
from .model import CLASS_MAP

from functools import partial

import logging
log = logging.getLogger(__name__)

__all__ = ["Client"]



import aioswagger11.client

from .model import Repository
from .model import Channel, Bridge, Playback, LiveRecording, StoredRecording, Endpoint, DeviceState, Sound

import logging
log = logging.getLogger(__name__)


class Client:
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
        self.class_map = CLASS_MAP.copy()

    async def __aenter__(self):
        await self._init()
        await self.nursery.start(self._run)
        return self

    async def __aexit__(self, *tb):
        with trio.fail_after(1) as scope:
            scope.shield=True
            await trio_asyncio.run_asyncio(self.close)

    def __enter__(self):
        raise RunimeError("You need to call 'async with …'.")

    def __exit__(self, *tb):
        raise RunimeError("You need to call 'async with …'.")

    def __iter__(self):
        raise RunimeError("You need to call 'async for …'.")

    def __aiter__(self):
        return ClientReader(self)

    async def _run(self, task_status=trio.TASK_STATUS_IGNORED):
        """Connect to the WebSocket and begin processing messages.

        This method will block until all messages have been received from the
        WebSocket, or until this client has been closed.

        :param apps: Application (or list of applications) to connect for
        :type  apps: str or list of str

        This is an asyncio 
        """
        apps = self._apps
        if isinstance(apps, list):
            apps = ','.join(apps)
        ws = await trio_asyncio.run_asyncio(partial(self.swagger.events.eventWebsocket, app=apps))
        self.websockets.add(ws)

        # For tests
        try:
            task_status.started()
            await self.__run(ws)
        finally:
            await ws.close()
            self.websockets.remove(ws)

    async def __run(self, ws):
        """Drains all messages from a WebSocket, sending them to the client's
        listeners.

        :param ws: WebSocket to drain.
        """
        while True:
            msg = await trio_asyncio.run_asyncio(ws.receive)
            if msg is None:
                return ## EOF
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING}:
                break
            elif msg.type != aiohttp.WSMsgType.TEXT:
                log.warning("Unknown JSON message type: %s", repr(msg))
                continue # ignore
            msg_json = json.loads(msg.data)
            if not isinstance(msg_json, dict) or 'type' not in msg_json:
                log.error("Invalid event: %s" % msg)
                continue
            await self.process_ws(msg_json)

    async def _init(self, RepositoryFactory=Repository):
        await trio_asyncio.run_asyncio(self.swagger.init)
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
        self.exception_handler = \
            lambda ex: log.exception("Event listener threw exception")

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
        :rtype:  trio_ari.model.Repository
        """
        return self.repositories.get(name)

    async def process_ws(self, msg):
        """Process one incoming websocket message.
        """
        msg = EventMessage(self, msg)

        # First, do the traditional listeners
        listeners = list(self.event_listeners.get(msg['type'], [])) \
                    + list(self.event_listeners.get('*', []))
        for listener in listeners:
            callback, args, kwargs = listener
            log.debug("cb_type=%s" % type(callback))
            args = args or ()
            kwargs = kwargs or {}
            cb = callback(msg, *args, **kwargs)
            if inspect.iscoroutine(cb):
                await cb

        # Next, dispatch the event to the objects in the message
        await msg._send_event()

    def on_event(self, event_type, event_cb, *args, **kwargs):
        """Register callback for events with given type.

        :param event_type: String name of the event to register for.
        :param event_cb: Callback function
        :type  event_cb: (dict) -> None
        :param args: Arguments to pass to event_cb
        :param kwargs: Keyword arguments to pass to event_cb
        """
        listeners = self.event_listeners.setdefault(event_type, list())
        for cb in listeners:
            if event_cb == cb[0]:
                listeners.remove(cb)
        callback_obj = (event_cb, args, kwargs)
        log.debug("event_cb=%s" % event_cb)
        listeners.append(callback_obj)
        client = self

        class EventUnsubscriber(object):
            """Class to allow events to be unsubscribed.
            """

            def close(self):
                """Unsubscribe the associated event callback.
                """
                if callback_obj in client.event_listeners[event_type]:
                    client.event_listeners[event_type].remove(callback_obj)

        return EventUnsubscriber()

    def on_object_event(self, event_type, event_cb, factory_fn, model_id,
                        *args, **kwargs):
        """Register callback for events with the given type. Event fields of
        the given model_id type are passed along to event_cb.

        If multiple fields of the event have the type model_id, a dict is
        passed mapping the field name to the model object.

        :param event_type: String name of the event to register for.
        :param event_cb: Callback function
        :type  event_cb: (Obj, dict) -> None or (dict[str, Obj], dict) ->
        :param factory_fn: Function for creating Obj from JSON
        :param model_id: String id for Obj from Swagger models.
        :param args: Arguments to pass to event_cb
        :param kwargs: Keyword arguments to pass to event_cb
        """
        # Find the associated model from the Swagger declaration
        log.debug("On object event %s %s %s %s"%(event_type, event_cb, factory_fn, model_id))
        event_model = self.event_models.get(event_type)
        if not event_model:
            raise ValueError("Cannot find event model '%s'" % event_type)

        # Extract the fields that are of the expected type
        obj_fields = [k for (k, v) in event_model['properties'].items()
                      if v['type'] == model_id]
        if not obj_fields:
            raise ValueError("Event model '%s' has no fields of type %s"
                             % (event_type, model_id))

        def extract_objects(event, *args, **kwargs):
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
            return event_cb(obj, event, *args, **kwargs)

        return self.on_event(event_type, extract_objects,
                             *args,
                             **kwargs)

    def on_channel_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Channel related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Channel, dict) -> None or (list[Channel], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Channel, 'Channel',
                                    *args, **kwargs)

    def on_bridge_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Bridge related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Bridge, dict) -> None or (list[Bridge], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Bridge, 'Bridge',
                                    *args, **kwargs)

    def on_playback_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Playback related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Playback, dict) -> None or (list[Playback], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Playback, 'Playback',
                                    *args, **kwargs)

    def on_live_recording_event(self, event_type, fn, *args, **kwargs):
        """Register callback for LiveRecording related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (LiveRecording, dict) -> None or (list[LiveRecording], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, LiveRecording,
                                    'LiveRecording', *args, **kwargs)

    def on_stored_recording_event(self, event_type, fn, *args, **kwargs):
        """Register callback for StoredRecording related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (StoredRecording, dict) -> None or (list[StoredRecording], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, StoredRecording,
                                    'StoredRecording', *args, **kwargs)

    def on_endpoint_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Endpoint related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Endpoint, dict) -> None or (list[Endpoint], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Endpoint, 'Endpoint',
                                    *args, **kwargs)

    def on_device_state_event(self, event_type, fn, *args, **kwargs):
        """Register callback for DeviceState related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (DeviceState, dict) -> None or (list[DeviceState], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, DeviceState, 'DeviceState',
                                    *args, **kwargs)

    def on_sound_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Sound related events

        :param event_type: String name of the event to register for.
        :param fn: Sound function
        :type  fn: (Sound, dict) -> None or (list[Sound], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Sound, 'Sound',
                                    *args, **kwargs)

class EventMessage:
    """This class encapsulates an event.
    All elements with known types are converted to objects,
    if a class for them is registered.
    """
    def __init__(self, client, msg):
        self._client = client
        self._orig_msg = msg

        event_type = msg['type']
        event_model = client.event_models.get(event_type)
        if not event_model:
            logger.warn("Cannot find event model '%s'" % event_type)
            return
        event_model = event_model.get('properties', {})

        for k, v in msg.items():
            setattr(self, k, v)

            m = event_model.get(k)
            if m is None:
                continue
            t = m['type']
            is_list = False
            m = re.match('''List\[(.*)\]''', t)
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


class ClientReader:
    link = None
    def __init__(self, client):
        self.client = client
        self.queue = trio.Queue(999)

    async def __anext__(self):
        if self.link is None:
            self.link = self.client.on_event('*', self._queue)
        return await self.queue.get()

    async def _queue(self, *a):
        await self.queue.put(a)

    async def aclose(self):
        if self.link is not None:
            self.link.close()
            self.link = None
        

