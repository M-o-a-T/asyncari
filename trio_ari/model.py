#!/usr/bin/env python

"""Model for mapping ARI Swagger resources and operations into objects.

The API is modeled into the Repository pattern, as you would find in Domain
Driven Design.

Each Swagger Resource (a.k.a. API declaration) is mapped into a Repository
object, which has the non-instance specific operations (just like what you
would find in a repository object).

Responses from operations are mapped into first-class objects, which themselves
have methods which map to instance specific operations (just like what you
would find in a domain object).

The first-class objects also have 'on_event' methods, which can subscribe to
Stasis events relating to that object.
"""

import re
import logging
import json
import inspect
from functools import partial
from weakref import WeakValueDictionary
from contextlib import suppress
import trio
import trio_asyncio

from aiohttp.web_exceptions import HTTPNoContent

log = logging.getLogger(__name__)


class StateError(RuntimeError):
    """The expected or waited-for state didn't occur"""

class EventTimeout(Exception):
    """There were no events past the timeout"""

class ResourceExit(Exception):
    """The resource does no longer exist."""

class ChannelExit(ResourceExit):
    """The channel has hung up."""

class BridgeExit(ResourceExit):
    """The bridge has terminated."""


class Repository(object):
    """ARI repository.

    This repository maps to an ARI Swagger resource. The operations on the
    Swagger resource are mapped to methods on this object, using the
    operation's nickname.

    :param client:  ARI client.
    :type  client:  client.Client
    :param name:    Repository name. Maps to the basename of the resource's
                    .json file
    :param resource:    Associated Swagger resource.
    :type  resource:    swaggerpy.client.Resource
    """

    def __init__(self, client, name, resource):
        self.client = client
        self.name = name
        self.api = resource

    def __repr__(self):
        return "Repository(%s)" % self.name

    def __getattr__(self, item):
        """Maps resource operations to methods on this object.

        :param item: Item name.

        This method needs to return a callable that returns a coroutine,
        which allows us to defer the attribute lookup.
        """
        class AttrOp:
            def __init__(self,p,item):
                self.p = p
                self.item = item
            def __repr__(self):
                return "<AttrOp<%s>.%s>" % (self.p, self.item)

            async def __call__(self, **kwargs):
                oper = getattr(self.p.api, self.item, None)
                if not (hasattr(oper, '__call__') and hasattr(oper, 'json')):
                    raise AttributeError(
                        "'%r' object has no attribute '%s'" % (self.p, self.item))
                jsc = oper.json
                if kwargs:
                    oper = partial(oper, **kwargs)
                res = await trio_asyncio.run_asyncio(oper)
                res = await promote(self.p.client, res, jsc)
                return res
        return AttrOp(self, item)
        #return lambda **kwargs: promote(self.client, oper(**kwargs), oper.json)


class ObjectIdGenerator(object):
    """Interface for extracting identifying information from an object's JSON
    representation.
    """

    def get_params(self, obj_json):
        """Gets the paramater values for specifying this object in a query.

        :param obj_json: Instance data.
        :type  obj_json: dict
        :return: Dictionary with paramater names and values
        :rtype:  dict of str, str
        """
        raise NotImplementedError("Not implemented")

    def id_as_str(self, obj_json):
        """Gets a single string identifying an object.

        :param obj_json: Instance data.
        :type  obj_json: dict
        :return: Id string.
        :rtype:  str
        """
        raise NotImplementedError("Not implemented")


# noinspection PyDocstring
class DefaultObjectIdGenerator(ObjectIdGenerator):
    """Id generator that works for most of our objects.

    :param param_name:  Name of the parameter to specify in queries.
    :param id_field:    Name of the field to specify in JSON.
    """

    def __init__(self, param_name, id_field='id'):
        self.param_name = param_name
        self.id_field = id_field

    def get_params(self, obj_json):
        return {self.param_name: obj_json[self.id_field]}

    def id_as_str(self, obj_json):
        return obj_json[self.id_field]


class BaseObject(object):
    """Base class for ARI domain objects.

    :param client:  ARI client.
    :type  client:  client.Client
    :param resource:    Associated Swagger resource.
    :type  resource:    swaggerpy.client.Resource
    :param json: JSON representation of this object instance.
    :type  json: dict
    """

    id_generator = ObjectIdGenerator()
    cache = None
    active = None
    id = None
    _queue = None
    json = None
    api = None
    state = None  # associated state machine
    _waiting = False  # protect against re-entering the event iterator

    def __new__(cls, client, id=None, json=None):
        if cls.cache is None:
            cls.cache = WeakValueDictionary()
        if cls.active is None:
            cls.active = set()
        if id is None:
            id = cls.id_generator.id_as_str(json)
        self = cls.cache.get(id)
        if self is not None:
            return self
        self = object.__new__(cls)
        cls.cache[id] = self
        self.json = {}
        return self

    def __init__(self, client, id=None, json=None):
        if self.api is None:
            raise RuntimeError("You need to override .api")
        if json is not None:
            # assume that the most-current event has the most-current JSON
            self.json.update(json)
        if self.id is not None:
            assert client == self.client
            return
        if id is None:
            id = self.id_generator.id_as_str(json)
        self.client = client
        self.api = getattr(self.client.swagger, self.api)
        self.id = id
        self.event_reg = {}
        self._init()

    def _init(self):
        pass

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.id)

    def __getattr__(self, item):
        """Promote resource operations related to a single resource to methods
        on this class.

        :param item:
        """
        return self._get_enriched(item)

    def _get_enriched(self, item):
        log.debug("Issuing command %s", item)
        oper = getattr(self.api, item, None)
        if not (hasattr(oper, '__call__') and hasattr(oper, 'json')):
            raise AttributeError(
                "'%r' object has no attribute '%r'" % (self, item))
        jsc = oper.json

        async def enrich_operation(**kwargs):
            """Enriches an operation by specifying parameters specifying this
            object's id (i.e., channelId=self.id), and promotes HTTP response
            to a first-class object.

            :param kwargs: Operation parameters
            :return: First class object mapped from HTTP response.
            """
            # Add id to param list
            kwargs.update(self.id_generator.get_params(self.json))
            oper_ = oper
            if kwargs:
                oper_ = partial(oper_, **kwargs)
            resp = await trio_asyncio.run_asyncio(oper_)
            enriched = await promote(self.client, resp, jsc)
            return enriched

        return enrich_operation


    async def create(self, **k):
        res = await self._get_enriched('create')(**k)
        type(self).active.add(res)
        return res
        
        
    def on_event(self, event_type, fn, *args, **kwargs):
        """Register event callbacks for this specific domain object.

        :param event_type: Type of event to register for.
        :type  event_type: str
        :param fn:  Callback function for events.
        :type  fn:  (object, dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        self.event_reg.setdefault(event_type, list()).append((fn, args, kwargs))

    async def do_event(self, msg):
        """Run a message through this object's event queue/list"""
        callbacks = self.event_reg.get(msg.type, []) + self.event_reg.get("*", [])
        for p, a, k in callbacks:
            r = p(self, msg, *a, **k)
            if inspect.iscoroutine(r):
                await r

        if self._queue is not None:
            self._queue.put_nowait(msg)
            # This is intentional: raise an error if the queue is full
            # instead of waiting forever and possibly deadlocking

    def __aiter__(self):
        if self._queue is None:
            self._queue = trio.Queue(99)
        return self

    async def __anext__(self):
        if self._queue is None:
            raise StopAsyncIteration
        if self._waiting:
            raise RuntimeError("Another task is waiting")
        try:
            self._waiting = True
            res = await self._queue.get()
        finally:
            self._waiting = False
        if res is None:
            self._queue = None
            raise StopAsyncIteration
        return res

    async def aclose(self):
        """No longer queue events"""
        if self._queue is not None:
            with suppress(trio.WouldBlock):
                self._queue.put_nowait(None)
        self._queue = None


class Channel(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('channelId')
    api = "channels"
    bridge = None

    def _init(self):
        super()._init()
        self.playbacks = set()
        self.vars = {}
        self._is_up = trio.Event()
        self._dtmf = trio.Queue(20)

    async def do_event(self, msg):
        if msg.type == "StasisStart":
            type(self).active.add(self)
        elif self not in type(self).active:
            return # unknown channel (program restarted?)
        elif msg.type == "StasisEnd":
            type(self).active.remove(self)
            self._is_up.set()
        elif msg.type == "ChannelVarset":
            self.vars[msg.variable] = msg.value
        elif msg.type == "ChannelEnteredBridge":
            self.bridge = msg.bridge
        elif msg.type == "ChannelLeftBridge":
            if self.bridge is msg.bridge:
                self.bridge = None
        elif msg.type == "PlaybackStarted":
            self.playbacks.add(msg.playback)
        elif msg.type == "PlaybackFinished":
            self.playbacks.remove(msg.playback)
        elif msg.type == "ChannelHangupRequest":
            self._is_up.set()
        elif msg.type == "ChannelStateChange":
            self._is_up.set()
        elif msg.type in {"ChannelDtmfReceived","ChannelHangupRequest","ChannelConnectedLine"}:
            pass
        else:
            log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)

    async def wait_up(self):
        await self._is_up.wait()
        if self.json['state'].lower() != "up":
            raise StateError(self)

    async def __anext__(self):
        evt = await super().__anext__()
        if evt.type in {"StasisEnd", "ChannelHangupRequest"}:
            raise ChannelExit(evt)
        return evt

class Bridge(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('bridgeId')
    api = "bridges"

    def _init(self):
        super()._init()
        self.playbacks = set()
        self.channels = set()

    async def do_event(self, msg):
        if msg.type == "BridgeDestroyed":
            try:
                type(self).active.remove(self)
            except KeyError:  # may or may not be ours
                pass
        elif msg.type == "BridgeMerged" and msg.bridge is not self:
            type(self).active.remove(self)
            type(self).cache[self.id] = msg.bridge
            msg.bridge.channels |= self.channels
            msg.bridge.playbacks |= self.playbacks
            for ch in self.channels:
                ch.bridge = msg.bridge
            for pb in self.playbacks:
                pb.bridge = msg.bridge
        elif msg.type == "ChannelEnteredBridge":
            self.channels.add(msg.channel)
        elif msg.type == "ChannelLeftBridge":
            self.channels.remove(msg.channel)
        elif msg.type == "PlaybackStarted":
            self.playbacks.add(msg.playback)
        elif msg.type == "PlaybackFinished":
            self.playbacks.remove(msg.playback)
        else:
            log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)

    async def __anext__(self):
        evt = await super().__anext__()
        if evt.type == "BridgeDestroyed":
            raise BridgeExit(evt)
        elif evt.type == "BridgeMerged" and msg.bridge is not self:
            self._queue = None
            raise StopAsyncIteration
        return evt


class Playback(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('playbackId')
    api = "playbacks"
    channel = None
    bridge = None

    def _init(self):
        self._is_playing = trio.Event()
        self._is_done = trio.Event()
        target = self.json.get('target_uri', '')
        if target.startswith('channel:'):
            self.channel = Channel(self.client, id=target[8:])
        elif target.startswith('bridge:'):
            self.bridge = Bridge(self.client, id=target[7:])

    async def do_event(self, msg):
        if self.channel is not None:
            await self.channel.do_event(msg)
        if self.bridge is not None:
            await self.bridge.do_event(msg)
        if msg.type == "PlaybackStarted":
            self._is_playing.set()
        elif msg.type == "PlaybackFinished":
            self._is_playing.set()
            self._is_done.set()
        else:
            log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)

    async def wait_playing(self):
        """Wait until the sound has started playing"""
        await self._is_playing.wait()

    async def wait_done(self):
        """Wait until the sound has stopped playing"""
        await self._is_done.wait()


class LiveRecording(BaseObject):
    """First class object API.

    :param client: ARI client
    :type  client: client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('recordingName', id_field='name')
    api = "recordings"

    async def do_event(self, msg):
        log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)


class StoredRecording(BaseObject):
    """First class object API.

    :param client: ARI client
    :type  client: client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('recordingName', id_field='name')
    api = "recordings"

    async def do_event(self, msg):
        log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)


# noinspection PyDocstring
class EndpointIdGenerator(ObjectIdGenerator):
    """Id generator for endpoints, because they are weird.
    """

    def get_params(self, obj_json):
        return {
            'tech': obj_json['technology'],
            'resource': obj_json['resource']
        }

    def id_as_str(self, obj_json):
        return "%(tech)s/%(resource)s" % self.get_params(obj_json)


class Endpoint(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """
    id_generator = EndpointIdGenerator()
    api = "endpoints"

    async def do_event(self, msg):
        log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)


class DeviceState(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('deviceName', id_field='name')
    endpoint = "deviceStates"

    async def do_event(self, msg):
        log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)


class Sound(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('soundId')
    endpoint = "sounds"

    async def do_event(self, msg):
        log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)


class Mailbox(BaseObject):
    """First class object API.

    :param client:       ARI client.
    :type  client:       client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('mailboxName', id_field='name')
    endpoint = "mailboxes"

    async def do_event(self, msg):
        log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)


async def promote(client, resp, operation_json):
    """Promote a response from the request's HTTP response to a first class
     object.

    :param client:  ARI client.
    :type  client:  client.Client
    :param resp:    aiohttp client resonse.
    :type  resp:    aiohttp.ClientResponse
    :param operation_json: JSON model from Swagger API.
    :type  operation_json: dict
    :return:
    """
    log.debug("resp1=%s",resp)
    if resp.status == HTTPNoContent.status_code:
        return None
    res = await trio_asyncio.run_asyncio(resp.text)
    if res == "":
        return None
    resp_json = json.loads(res)
    log.debug("resp2=%s",resp_json)

    response_class = operation_json['responseClass']
    is_list = False
    m = re.match('''List\[(.*)\]''', response_class)
    if m:
        response_class = m.group(1)
        is_list = True
    factory = CLASS_MAP.get(response_class)
    if factory:
        if is_list:
            return [factory(client, json=obj) for obj in resp_json]
        return factory(client, json=resp_json)
    log.info("No mapping for %s; returning JSON" % response_class)
    return resp_json


CLASS_MAP = {
    'Bridge': Bridge,
    'Channel': Channel,
    'Endpoint': Endpoint,
    'Playback': Playback,
    'LiveRecording': LiveRecording,
    'StoredRecording': StoredRecording,
    'Mailbox': Mailbox,
    'DeviceState': DeviceState,
}
