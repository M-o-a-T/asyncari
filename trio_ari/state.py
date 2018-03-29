"""
Basic state machine for ARI channels.

The principle is very simple: On entering a state, :meth:`State.run` is
called. Exiting the state passes control back to the caller. If the channel
hangs up, a :class:`ChannelExit` exception is raised.
"""

import math
import trio

from .model import ChannelExit, BridgeExit, EventTimeout, StateError

import logging
logger = logging.getLogger(__name__)

__all__ = ["ToplevelChanelstate", "Channelstate", "Bridgestate", "HangupBridgestate", "OutgoingChannelState"]

_StartEvt = "_StartEvent"

class _EvtHandler:
	timeout = math.inf
	result = None
	_startup = True

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

	async def on_start(self):
		"""Called when the state machine starts up.
		Defaults to doing nothing.
		"""
		pass

	async def on_timeout(self):
		"""Called when no event arrives after ``self.timeout`` seconds.
		Raises :class:`EventTimeout` when the timeout isn't set to
		:item:`math.inf`..
		"""
		raise EventTimeout(self)
		

class BridgeState(_EvtHandler):
	def __init__(self, bridge):
		self.bridge = bridge
		self.client = self.channel.client

	async def run(self):
		"""Process events arriving on this channel.
		
		By default, call :meth:`dispatch` with each event.
		"""
		async for evt in self._evt(self, self.bridge):
			try:
				if evt is _StartEvt:
					await self.on_start()
				else:
					await self.dispatch(evt)
			except StopAsyncIteration:
				return self.result
	
	async def dispatch(self, evt):
		"""Dispatch a single event.

		By default, call ``on_EventName(evt)``.
		If the event name starts with "Bridge", this prefix is stripped.
		"""
		typ = evt.type
		if typ.startswith('Bridge'):
			typ = typ[6:]
		try:
			handler = getattr(self, 'on_'+typ)
		except AttributeError:
			logger.warn("Unhandled event %s on %s", evt, self.channel)
		else:
			await handler(evt)

	async def on_timeout(self):
		raise StopAsyncIteration

	async def on_Merged(self, evt):
		if evt.bridge is not self.bridge:
			raise StopAsyncIteration

class HangupBridgeState(BridgeState):
	"""A bridge controller that removes all channels and deletes the
	bridge as soon as one channel leaves.
	"""
	def __init__(self, bridge, timeout=math.inf):
		super().__init__(bridge)
		if self.bridge.channels < 2:
			self.timeout = timeout
		
	async def on_ChannelEnteredBridge(self, evt):
		if self.bridge.channels >= 2:
			self.timeout = math.inf
	
	async def on_ChannelLeftBridge(self, evt):
		for ch in self.bridge.channels:
			if ch is not evt.channel:
				await self.bridge.removeChannel(ch)
		await self.bridge.destroy()
	
class ChannelState(_EvtHandler):
	def __init__(self, channel):
		self.channel = channel
		self.client = self.channel.client
	
	async def run(self):
		"""Process events arriving on this channel.
		
		By default, call :meth:`dispatch` with each event.
		"""
		async for evt in self._evt(self, self.channel):
			try:
				if evt is _StartEvt:
					await self.on_start()
				else:
					await self.dispatch(evt)
			except StopAsyncIteration:
				return self.result
	
	async def dispatch(self, evt):
		"""Dispatch a single event.

		By default, call ``on_EventName(evt)``.
		If the event name starts with "Channel", this prefix is stripped.
		"""
		typ = evt.type
		if typ.startswith('Channel'):
			typ = typ[7:]
		try:
			handler = getattr(self, 'on_'+typ)
		except AttributeError:
			logger.warn("Unhandled event %s on %s", evt, self.channel)
		else:
			await handler(evt)

	async def on_DtmfReceived(self, evt):
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
			logger.info("Unhandled DTMF %s on %s", evt.digit, self.channel)
		else:
			await proc(evt)

class ToplevelChannelState(ChannelState):
	"""A channel state machine that unconditionally hangs up its channel on exception"""
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		task_status.started()
		try:
			await super().run()
		except ChannelExit:
			pass
		finally:
			with trio.move_on_after(2) as s:
				s.shield = True
				try:
					await self.channel.hangup()
				except Exception as exc:
					logger.info("Channel %s gone: %s", self.channel, exc)

class OutgoingChannelState(ToplevelChannelState):
	"""A channel state machine that waits for an initial StasisStart event before proceeding"""
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		async for evt in self.channel:
			if evt.type != "StatisStart":
				raise StateError(evt)
			break
		task_status.started()
		await super().run()

