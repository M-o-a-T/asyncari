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
import anyio
from asks.errors import BadStatus
from .util import mayNotExist

log = logging.getLogger(__name__)

NO_CONTENT = 204

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

class OperationError(RuntimeError):
    """Some Swagger operation failed"""

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
                try:
                    res = await oper(**kwargs)
                except BadStatus as exc:
                    raise OperationError(getattr(exc,'data',{'message':getattr(exc,'body',"")})['message']) from exc

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


QLEN=99

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
    _q = None
    _qlen = 0
    json = None
    api = None
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
        self.event_listeners = {}
        self._changed = anyio.create_event()
        self._init()

    def _init(self):
        pass

    async def wait_for(self, check):
        while True:
            r = check()
            if r:
                return r
            await self._changed.wait()

    async def _has_changed(self):
        c,self._changed = self._changed,anyio.create_event()
        await c.set()

    def remember(self):
        """
        Call this method after you created a persistent object.

        This will ensure that Python won't forget about it even if you
        don't keep a reference to it yourself.
        """
        type(self).active.add(self)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.id)

    def __getattr__(self, item):
        """Promote resource operations related to a single resource to methods
        on this class.

        :param item:
        """
        try:
            return self.json[item]
        except KeyError:
            return self._get_enriched(item)

    def _get_enriched(self, item):
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
            log.debug("Issuing command %s %s", item, kwargs)
            oper_ = oper
            resp = await oper_(**kwargs)
            enriched = await promote(self.client, resp, jsc)
            return enriched

        return enrich_operation


    async def create(self, **k):
        res = await self._get_enriched('create')(**k)
        type(res).active.add(res)
        return res
        
        
    def on_event(self, event_type, fn, *args, **kwargs):
        """Register event callbacks for this specific domain object.

        :param event_type: Type of event to register for, or '*'
        :type  event_type: str
        :param fn:  Callback function for events: fn(evt, *args, **kwargs)
        :type  fn:  (object, dict) -> None

        All additional arguments or keywords are passed to `fn`.

        The return value is an object with a `close` method; call it to
        deregister the event handler.
        """
        client = self.client
        callback_obj = (fn, args, kwargs)
        self.event_listeners.setdefault(event_type, list()).append(callback_obj)

        class EventUnsubscriber(object):
            """Class to allow events to be unsubscribed.
            """

            def close(self_):
                """Unsubscribe the associated event callback.
                """
                try:
                    self.event_listeners[event_type].remove(callback_obj)
                except ValueError:
                    pass

        return EventUnsubscriber()


    async def do_event(self, msg):
        """Run a message through this object's event queue/list"""
        callbacks = self.event_listeners.get(msg.type, []) + self.event_listeners.get("*", [])
        for p, a, k in callbacks:
            log.debug("RunCb:%s %s %s %s",self,p,a,k)
            r = p(msg, *a, **k)
            if inspect.iscoroutine(r):
                await r

        if self._q is not None:
            if self._q_len >= QLEN-1:
                raise RuntimeError("queue full")
            self._q_len += 1
            await self._q.put(msg)

        # Finally trigger waiting checks
        await self._has_changed()

    def __aiter__(self):
        if self._q is None:
            self._q = anyio.create_queue(QLEN)
        return self

    async def __anext__(self):
        if self._q is None:
            raise StopAsyncIteration
        if self._waiting:
            self._waiting = False
            raise RuntimeError("Another task is waiting")
        try:
            self._waiting = True
            res = await self._q.get()
            self._q_len -= 1
        finally:
            if not self._waiting:
                raise RuntimeError("Another task has waited")
            self._waiting = False
        return res

    async def aclose(self):
        """No longer queue events"""
        if self._q is not None:
            await self._q.put(None)
            self._q = None


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
    _do_hangup = None
    hangup_delay=0.3
    _reason = None
    _reason_seen = None
    prev_state = None
    _state = None

    # last is better
    REASONS = ( "congestion", "no_answer", "busy", "normal" )

    def _init(self):
        super()._init()
        self.playbacks = set()
        self.vars = {}

    async def set_reason(self, reason):
        """Set the reason for hanging up."""

        if reason not in self.REASONS:
            raise RuntimeError("Reason '%s' unknown" % (reason,))
        if self._reason is None:
            self._reason = reason
        elif self.REASONS.index(reason) > self.REASONS.index(self._reason):
            self._reason = reason

        self._reason = reason
        if self._reason_seen is not None:
            await self._reason_seen.set()

    async def hang_up(self, reason=None):
        """Call this (and only this) to hang up a channel.

        The actual hangup happens asynchronously.
        """
        if self._do_hangup is not None:
            return
        self._do_hangup = True
        if reason is not None:
            await self.set_reason(reason)
        if self._reason is None:
            self._reason_seen = anyio.create_event()
        await self.client.taskgroup.spawn(self._hangup_task)

    async def exit_hangup(self, reason="normal"):
        """Hang up on exit.

        Override this to be a no-op if you want to redirect the
        channel to a non-Stasis dialplan entry instead.
        """
        try:
            if self._do_hangup is not False:
                with mayNotExist:
                    await self.hangup(reason=reason)
        finally:
            self.state = "Gone"
            await self._changed.set()

    async def _hangup_task(self, evt: anyio.abc.Event = None):
        if evt is not None:
            await evt.set()
        if self._reason is None:
            async with anyio.move_on_after(self.hangup_delay):
                await self._reason_seen.wait()

        try:
            await self.exit_hangup(reason=(self._reason or "normal"))
        except Exception as exc:
            log.warning("Hangup %s: %s", self, exc)

        self._do_hangup = False

    async def do_event(self, msg):
        if msg.type == "StasisStart":
            type(self).active.add(self)
        elif msg.type in {"StasisEnd", "ChannelDestroyed"}:
            try:
                type(self).active.remove(self)
            except KeyError:
                pass
            self._do_hangup = False
        elif msg.type == "ChannelVarset":
            self.vars[msg.variable] = msg.value
        elif msg.type == "ChannelEnteredBridge":
            self.bridge = msg.bridge
        elif msg.type == "ChannelLeftBridge":
            if self.bridge is msg.bridge:
                self.bridge = None

        elif msg.type == "PlaybackStarted":
            assert msg.playback not in self.playbacks
            self.playbacks.add(msg.playback)
        elif msg.type == "PlaybackFinished":
            try:
                self.playbacks.remove(msg.playback)
            except KeyError:
                log.warning("%s not in %s", msg.playback, self)

        elif msg.type == "ChannelHangupRequest":
            pass
        elif msg.type == "ChannelConnectedLine":
            pass
        elif msg.type == "ChannelStateChange":
            log.debug("State:%s %s", self.state, self)
            pass
        elif msg.type == "ChannelDtmfReceived":
            pass
        else:
            log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)

    async def wait_up(self):
        def chk():
            return self.state == "Up"
        await self.wait_for(chk)

    async def wait_bridged(self, bridge=None):
        """\
            Wait for the channel to be bridged to @bridge.

            if None, wait for the channel to be connected to any bridge.
            """
        def chk():
            if self._do_hangup is not None:
                raise StateError(self)
            if bridge is None:
                return self.bridge is not None
            else:
                return self.bridge is bridge
        await self.wait_for(chk)

    async def wait_not_bridged(self, bridge=None):
        """\
            Wait for the channel to no longer be bridged to @bridge.

            if None, wait for the channel to be not connected to any bridge.
            """
        def chk():
            if bridge is None:
                return self.bridge is None
            else:
                return self.bridge is not bridge
        await self.wait_for(chk)

    async def wait_not_playing(self):
        """\
            Wait until all sound playbacks are finished.
            """
        await self.wait_for(lambda: not self.playbacks)

    async def wait_down(self):
        await self.wait_for(lambda: self._do_hangup is False)

    async def __anext__(self):
        evt = await super().__anext__()
        if evt.type in {"StasisEnd", "ChannelDestroyed"}:
            raise StopAsyncIteration
        return evt

class Bridge(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    ;param id: Instance ID, if JSON is not yet known
    :param json: Instance data

    Warning: a bridge is not auto-deleted when the last channel leaves
    or when your program ends! Your code needs to do that on its own.

    Unique bridges should have well-known IDs so that they can be
    reconnected to if your program is restarted.
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
            assert msg.channel not in self.channels
            self.channels.add(msg.channel)
        elif msg.type == "ChannelLeftBridge":
            try:
                self.channels.remove(msg.channel)
            except KeyError:
                log.warning("%s not in %s", msg.channel, self)
        elif msg.type == "PlaybackStarted":
            assert msg.playback not in self.playbacks
            self.playbacks.add(msg.playback)
        elif msg.type == "PlaybackFinished":
            try:
                self.playbacks.remove(msg.playback)
            except KeyError:
                log.warning("%s not in %s", msg.playback, self)
        else:
            log.warn("Event not recognized: %s for %s", msg, self)
        await super().do_event(msg)

        if hasattr(msg,'bridge'):
            for ch in self.channels - msg.bridge.channels:
                log.warn("%s: %s not listed",self,ch)
            for ch in msg.bridge.channels - self.channels:
                log.warn("%s: %s not known",self,ch)

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
        self._is_playing = anyio.create_event()
        self._is_done = anyio.create_event()
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
            await self._is_playing.set()
        elif msg.type == "PlaybackFinished":
            await self._is_playing.set()
            await self._is_done.set()
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
    :param resp:    asks response.
    :type  resp:    asks.Response
    :param operation_json: JSON model from Swagger API.
    :type  operation_json: dict
    :return:
    """
    if resp.status_code == NO_CONTENT:
        log.debug("resp=%s",resp)
        return None
    res = resp.text
    if res == "":
        log.debug("resp=%s (empty)",resp)
        return None
    resp_json = json.loads(res)
    log.debug("resp=%s",resp_json)

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
    log.info("No mapping for %s" % response_class)
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
