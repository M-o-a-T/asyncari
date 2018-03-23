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
import trio_asyncio

from aiohttp.web_exceptions import HTTPNoContent

log = logging.getLogger(__name__)


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
    :param as_json: JSON representation of this object instance.
    :type  as_json: dict
    :param event_reg:
    """

    id_generator = ObjectIdGenerator()

    def __init__(self, client, resource, as_json, event_reg):
        self.client = client
        self.api = resource
        self.json = as_json
        self.id = self.id_generator.id_as_str(as_json)
        self.event_reg = event_reg

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.id)

    def __getattr__(self, item):
        """Promote resource operations related to a single resource to methods
        on this class.

        :param item:
        """
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

    def on_event(self, event_type, fn, *args, **kwargs):
        """Register event callbacks for this specific domain object.

        :param event_type: Type of event to register for.
        :type  event_type: str
        :param fn:  Callback function for events.
        :type  fn:  (object, dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """

        async def fn_filter(objects, event, *args, **kwargs):
            """Filter received events for this object.

            :param objects: Objects found in this event.
            :param event: Event.
            """
            res = None
            if isinstance(objects, dict):
                if self.id in [c.id for c in objects.values()]:
                    res = fn(objects, event, *args, **kwargs)
            else:
                if self.id == objects.id:
                    res = fn(objects, event, *args, **kwargs)
            # The callback may or may not be an async function
            if inspect.iscoroutine(res):
                await res
            return res


        if not self.event_reg:
            msg = "Event callback registration called on object with no events"
            raise RuntimeError(msg)

        return self.event_reg(event_type, fn_filter, *args, **kwargs)


class Channel(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    :param channel_json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('channelId')

    def __init__(self, client, channel_json):
        super(Channel, self).__init__(
            client, client.swagger.channels, channel_json,
            client.on_channel_event)


class Bridge(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    :param bridge_json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('bridgeId')

    def __init__(self, client, bridge_json):
        super(Bridge, self).__init__(
            client, client.swagger.bridges, bridge_json,
            client.on_bridge_event)


class Playback(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    :param playback_json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('playbackId')

    def __init__(self, client, playback_json):
        super(Playback, self).__init__(
            client, client.swagger.playbacks, playback_json,
            client.on_playback_event)


class LiveRecording(BaseObject):
    """First class object API.

    :param client: ARI client
    :type  client: client.Client
    :param recording_json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('recordingName', id_field='name')

    def __init__(self, client, recording_json):
        super(LiveRecording, self).__init__(
            client, client.swagger.recordings, recording_json,
            client.on_live_recording_event)


class StoredRecording(BaseObject):
    """First class object API.

    :param client: ARI client
    :type  client: client.Client
    :param recording_json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('recordingName', id_field='name')

    def __init__(self, client, recording_json):
        super(StoredRecording, self).__init__(
            client, client.swagger.recordings, recording_json,
            client.on_stored_recording_event)


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
    :param endpoint_json: Instance data
    """
    id_generator = EndpointIdGenerator()

    def __init__(self, client, endpoint_json):
        super(Endpoint, self).__init__(
            client, client.swagger.endpoints, endpoint_json,
            client.on_endpoint_event)


class DeviceState(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    :param endpoint_json: Instance data
    """
    id_generator = DefaultObjectIdGenerator('deviceName', id_field='name')

    def __init__(self, client, device_state_json):
        super(DeviceState, self).__init__(
            client, client.swagger.deviceStates, device_state_json,
            client.on_device_state_event)


class Sound(BaseObject):
    """First class object API.

    :param client:  ARI client.
    :type  client:  client.Client
    :param sound_json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('soundId')

    def __init__(self, client, sound_json):
        super(Sound, self).__init__(
            client, client.swagger.sounds, sound_json, client.on_sound_event)


class Mailbox(BaseObject):
    """First class object API.

    :param client:       ARI client.
    :type  client:       client.Client
    :param mailbox_json: Instance data
    """

    id_generator = DefaultObjectIdGenerator('mailboxName', id_field='name')

    def __init__(self, client, mailbox_json):
        super(Mailbox, self).__init__(
            client, client.swagger.mailboxes, mailbox_json, None)


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
    log.debug("resp=%s",resp)
    if resp.status == HTTPNoContent.status_code:
        return None
    res = await trio_asyncio.run_asyncio(resp.text)
    if res == "":
        return None
    resp_json = json.loads(res)

    response_class = operation_json['responseClass']
    is_list = False
    m = re.match('''List\[(.*)\]''', response_class)
    if m:
        response_class = m.group(1)
        is_list = True
    factory = CLASS_MAP.get(response_class)
    if factory:
        if is_list:
            return [factory(client, obj) for obj in resp_json]
        return factory(client, resp_json)
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
