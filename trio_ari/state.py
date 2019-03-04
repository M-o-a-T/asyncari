"""
Basic state machine for ARI channels.

The principle is very simple: On entering a state, :meth:`State.run` is
called. Exiting the state passes control back to the caller. If the channel
hangs up, a :class:`ChannelExit` exception is raised.
"""

import math
import trio
import inspect

from .model import ChannelExit, BridgeExit, EventTimeout, StateError
from async_generator import asynccontextmanager

import logging
log = logging.getLogger(__name__)

__all__ = ["ToplevelChannelstate", "Channelstate", "Bridgestate", "HangupBridgestate", "OutgoingChannelState"]

_StartEvt = "_StartEvent"

CAUSE_MAP = {
	1: "congestion",
	2: "congestion",
	3: "congestion",
	16: "normal",
	17: "busy",
	18: "no_answer",
	19: "no_answer", # but ringing
	21: "busy", # rejected
	27: "congestion",
	34: "congestion",
	38: "congestion",
}

class _EvtHandler:
	"""This is our generic state machine implementation."""

	timeout = math.inf
	result = None
	_startup = True
	_src = None # attribute name; "bridge" or "channel" 

	class _evt:
		def __init__(evt, self, src):
			evt.self = self
			evt.src = src

		def __aiter__(evt):
			evt.it = evt.src.__aiter__()
			return evt

		async def __anext__(evt):
			if evt.self._startup:
				evt.self._startup = False
				return _StartEvt
			while True:
				try:
					with trio.fail_after(evt.self.timeout):
						res = await evt.it.__anext__()
					return res
				except trio.TooSlowError:
					await evt.self.on_timeout()

	async def _dispatch(self, evt):
		"""Dispatch a single event.

		By default, call ``self.on_EventName(evt)``.
		"""
		typ = evt.type
		try:
			handler = getattr(self, 'on_'+typ)
		except AttributeError:
			log.warn("Unhandled event %s on %s", evt, self)
		else:
			await handler(evt)

	def _repr(self):
		"""List of attribute+value pairs to include in ``repr``."""
		res = []
		if self._src:
			res.append((self._src, getattr(self,self._src)))
		return res

	def __repr__(self):
		return "<%s: %s>" % (self.__class__.__name__, ','.join("%s=%s"%(a,b) for a,b in self._repr()))

	async def on_start(self):
		"""Called when the state machine starts up.
		Defaults to doing nothing.
		"""
		pass

	async def on_timeout(self):
		"""Called when no event arrives after ``self.timeout`` seconds.
		Raises :class:`EventTimeout`.
		"""
		raise EventTimeout(self)

	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		"""Process events arriving on this channel.
		
		By default, call :meth:`_dispatch` with each event.
		"""
		log.debug("StartRun %s", self)
		task_status.started()
		async for evt in self._evt(self, getattr(self, self._src)):
			try:
				log.debug("EvtRun:%s %s", evt, self)
				if evt is _StartEvt:
					await self.on_start()
				else:
					await self._dispatch(evt)
			except StopAsyncIteration:
				log.debug("StopRun %s", self)
				return self.result


class _DTMFevtHandler(_EvtHandler):
	"""Extension to dispatch DTMF tones."""

	async def on_ChannelDtmfReceived(self, evt):
		"""Dispatch DTMF events.

		Call ``on_dtmf_{0-9,A-E,Hash,Pound}`` methods.
		If that doesn't exist and a digit is dialled, call ``on_dtmf_digit``.
		If that doesn't exist either, call ``on_dtmf``.
		If that doesn't exist either, log and ignore.
		"""

		digit = evt.digit
		if digit == '#':
			digit = 'Hash'
		elif digit == '*':
			digit = 'Pound'
		proc = getattr(self,'on_dtmf_'+digit, None)
		if proc is None and digit >= '0' and digit <= '9':
			proc = getattr(self,'on_dtmf_digit', None)
		if proc is None:
			proc = getattr(self,'on_dtmf', None)
		if proc is None:
			log.info("Unhandled DTMF %s on %s", evt.digit, self.ref)
		else:
			p = proc(evt)
			if inspect.iscoroutine(p):
				await p


class ChannelState(_DTMFevtHandler):
	"""This is the generic state machine for a single channel."""
	_src = 'channel'
	def __init__(self, channel):
		self.channel = channel
		self.client = self.channel.client

	def _repr(self):
		res=super()._repr()
		res.append(("ch_state",self.channel.state))
		return res

	@property
	def ref(self):
		return self.channel

class BridgeState(_DTMFevtHandler):
	"""
	This is the generic state machine for a bridge.

	This bridge raises BridgeExit when it terminates.
	"""
	_src = 'bridge'
	TYPE="mixing"
	calls = set()

	def __init__(self, bridge):
		self.bridge = bridge
		self.client = self.bridge.client

	@property
	def ref(self):
		return self.bridge

	@classmethod
	async def new(cls, client, type="mixing", **kw):
		br = await client.bridges.create(type=type)
		s = cls(br, **kw)
		client.nursery.start_soon(s.run)
		return s

	async def add(self, channel):
		"""Add a new channel to this bridge."""
		await self._add_monitor(channel)
		await self.bridge.addChannel(channel=channel.id)
		await channel.wait_bridged(self.bridge)
		
	async def on_channel_added(self, channel):
		"""Hook, called after a channel has been added successfully."""
		pass

	async def remove(self, channel):
		"""Remove a channel from this bridge."""
		await self.bridge.removeChannel(channel=channel.id)
		await channel.wait_bridged(None)
		
	async def _dial(self, State=ChannelState, **kw):
		"""Helper to start a call"""
		ch_id = self.client.generate_id()
		log.debug("DIAL %s",kw.get('endpoint', 'unknown'))
		ch = await self.client.channels.originate(channelId=ch_id, app=self.client._app, appArgs=["dialed", kw.get('endpoint', 'unknown')], **kw)
		self.calls.add(ch)
		ch.remember()
		await self._add_monitor(ch)
		return ch

	async def dial(self, State=None, **kw):
		"""
		Originate a call. Add the called channel to this bridge.

		State: the state machine (factory) to run the new channel under.

		Returns a state instance (if given), or the channel (if not).
		"""
		
		ch = await self._dial(**kw)
		try:
			await ch.wait_up()
		except BaseException:
			with trio.move_on_after(2) as s:
				s.shield = True
				await ch.hang_up()
				await ch.wait_down()
			raise

		if State is None:
			return ch
		else:
			s = State(ch)
			await self.client.nursery.start(s.run)
			return s

	async def calling(self, State=None, timeout=None, **kw):
		"""
		Context manager for an outgoing call.

		The context is entered as the call is established. It is
		auto-terminated when the context ends.

		Usage::

			with bridge.calling(endpoint="SIP/foo/0123456789", timeout=60) as channel:
				channel.play(media='sound:hello-world')

		The timeout only applies to the call setup.

		If a state machine (factory) is passed in, it will be instantiated
		run during the call.

		"""
		return CallManager(self, State=State, timeout=timeout, **kw)

	async def on_StasisStart(self, evt):
		"""Hook for channel creation. Call when overriding!"""
		ch = evt.channel
		await self.bridge.addChannel(channel=ch.id)
#		if evt.channel not in self.bridge.channels:
#			await self.bridge.addChannel(channel=[evt.channel.id])

	async def on_connected(self, channel):
		"""Callback when an outgoing call is answered.
		Default: answer all (incoming) channels that are still in RING
		"""
		for ch in self.bridge.channels:
			if ch.state == "Ring":
				await ch.answer()

	async def on_timeout(self):
		"""Timeout handler. Default: terminate the state machine."""
		raise StopAsyncIteration

	async def on_BridgeMerged(self, evt):
		if evt.bridge is not self.bridge:
			raise StopAsyncIteration

	async def on_ChannelEnteredBridge(self, evt):
		# We need to keep track of the channel's state
		ch = evt.channel
		try:
			self.calls.remove(ch)
		except KeyError:
			pass
		await self.on_channel_added(ch)
		if ch.state == "Up":
			await self.on_connected(ch)

	async def on_ChannelLeftBridge(self, evt):
		await self._chan_dead(evt)
	
	async def _add_monitor(self, ch):
		if not hasattr(ch,'_bridge_evt'):
			ch._bridge_evt = ch.on_event("*", self._chan_evt)

	async def _chan_evt(self, evt):
		"""Dispatcher for forwarding a channel's events to this bridge."""
		if getattr(evt,'bridge',None) is self:
			log.debug("Dispatch hasBRIDGE:%s for %s",evt.type,self)
			return # already calling us via regular dispatch
		p = getattr(self, 'on_'+evt.type, None)
		if p is None:
			log.debug("Dispatch UNKNOWN:%s for %s",evt.type,self)
			return
		log.debug("Dispatch %s for %s",evt.type,self)
		p = p(evt)
		if inspect.iscoroutine(p):
			await p

	async def on_ChannelStateChange(self, evt):
		await self._chan_state_change(evt)

	async def on_ChannelConnectedLine(self, evt):
		await self._chan_state_change(evt)

	async def on_ChannelDestroyed(self, evt):
		self._set_cause(evt)
		await self._chan_dead(evt)

	async def on_ChannelHangupRequest(self, evt):
		self._set_cause(evt)
		return  # TODO?
		try:
			await evt.channel.hang_up()
		except Exception as exc:
			log.warning("Hangup %s: %s", evt.channel, exc)
		# await self._chan_dead(evt)

	async def on_channel_end(self, ch, evt=None):
		"""
		The connection to this channel ended.

		Overrideable, but do call ``await super().on_channel_end(ch,evt)`` first.
		"""
		try:
			self.calls.remove(ch)
		except KeyError:
			pass

	async def _set_cause(self, evt):
		try:
			cc = evt.cause
		except AttributeError:
			pass
		else:
			cc = CAUSE_MAP.get(cc,"normal")
			for c in list(self.bridge.channels)+list(self.calls):
				c.set_reason(cc)

	async def _chan_dead(self, evt):
		ch = evt.channel

		if not hasattr(ch, '_bridge_evt'):
			return
		ch._bridge_evt.close()
		del ch._bridge_evt

		await self.on_channel_end(ch, evt)

	async def _chan_state_change(self, evt):
		ch = evt.channel
		log.debug("StateChange %s %s", self, ch)
		if ch not in self.bridge.channels:
			return
		if ch.state == "Up":
			await self.on_connected(ch)

	async def teardown(self, skip_ch=None, hangup_reason="normal"):
		"""removes all channels from the bridge"""
		if self._in_shutdown:
			return
		self._in_shutdown = True

		log.info("TEARDOWN %s %s",self,self.bridge.channels)
		for ch in list(self.bridge.channels)+list(self.calls):
			if hangup_reason:
				try:
					await ch.hang_up(reason=hangup_reason)
				except Exception as exc:
					log.info("%s gone: %s", ch, exc)
			if ch is skip_ch:
				continue
			try:
				await self.bridge.removeChannel(channel=ch.id)
			except Exception as exc:
				log.info("%s detached: %s", ch, exc)
				
		await self.bridge.destroy()


class ToplevelBridgeState(BridgeState):
	"""A bridge state suitable for an incoming channel.
	"""
	@classmethod
	@asynccontextmanager
	async def new(cls, client, nursery=None, type="mixing", **kw):
		br = await client.bridges.create(type=type)
		s = cls(br, **kw)
		s.nursery = nursery or client.nursery
		try:
			await s.nursery.start(s.run)
			yield s
		finally:
			await s.teardown()
			# s.nursery.cancel_scope.cancel()

		return
		

class HangupBridgeState(ToplevelBridgeState):
	"""A bridge controller that hangs up all channels and deletes the
	bridge as soon as one channel leaves.

	This state machine does not raise BridgeExit.
	"""
	_in_shutdown = None

	def __init__(self, bridge, join_timeout=math.inf, talk_timeout=math.inf):
		super().__init__(bridge)
		self.talk_timeout = talk_timeout
		if len(self.bridge.channels) >= 2:
			self.timeout = talk_timeout
		else:
			self.timeout = join_timeout

	async def on_ChannelEnteredBridge(self, evt):
		await super().on_ChannelEnteredBridge(evt)

		if len(self.bridge.channels) >= 2:
			self.timeout = self.talk_timeout

	async def on_channel_end(self, ch, evt=None):
		await super().on_channel_end(ch, evt)
		await self.teardown(ch)

	async def on_timeout(self):
		await self.teardown()

	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		try:
			return await super().run(task_status=task_status)
		except StateError:
			await self.teardown()
		except BridgeExit:
			pass


class ToplevelChannelState(ChannelState):
	"""A channel state machine that unconditionally hangs up its channel on exception"""
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		"""Task for this state. Hangs up the channel on exit."""
		with trio.open_cancel_scope() as scope:
			self._scope = scope
			try:
				await super().run(task_status=task_status)
			except ChannelExit:
				pass
			except StateError:
				pass
			finally:
				with trio.move_on_after(2) as s:
					s.shield = True
					try:
						await self.channel.hang_up()
						await self.channel.wait_down()
					except Exception as exc:
						log.info("Channel %s gone: %s", self.channel, exc, exc_info=exc)

	async def hang_up(self, reason="normal"):
		await self.channel.set_reason(reason)
		self._scope.cancel()

class OutgoingChannelState(ToplevelChannelState):
	"""A channel state machine that waits for an initial StasisStart event before proceeding"""
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		async for evt in self.channel:
			if evt.type != "StatisStart":
				raise StateError(evt)
			break
		task_status.started()
		await super().run()

class CallManager:
	state = None
	channel = None

	def __init__(self, bridge, State=None, timeout=None, **kw):
		self.bridge = bridge
		self.State = State
		self.timeout = timeout
		self.kw = kw
	
	async def __aenter__(self):
		timeout = self.timeout
		if timeout is None:
			timeout = math.inf

		with trio.fail_after(timeout):
			self.channel = ch = self.bridge.dial(**self.kw)
		if self.State is not None:
			try:
				self.state = state = self.State(ch)
				await self.bridge.nursery.start(state.main)
			except BaseException:
				with trio.open_cancel_scope(shield=True):
					await ch.hangup()
				raise

	async def __aexit__(self, *exc):
		if self.state is None:
			await self.state.hang_up()
		else:
			await self.channel.hang_up()

