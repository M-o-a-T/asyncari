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

import logging
log = logging.getLogger(__name__)

__all__ = ["ToplevelChannelstate", "Channelstate", "Bridgestate", "HangupBridgestate", "OutgoingChannelState"]

_StartEvt = "_StartEvent"

class _EvtHandler:
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

	def _repr(self):
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
		Raises :class:`EventTimeout` when the timeout isn't set to
		:item:`math.inf`..
		"""
		raise EventTimeout(self)

	async def run(self):
		"""Process events arriving on this channel.
		
		By default, call :meth:`dispatch` with each event.
		"""
		log.debug("StartRun %s", self)
		async for evt in self._evt(self, getattr(self, self._src)):
			try:
				log.debug("EvtRun:%s %s", evt, self)
				if evt is _StartEvt:
					await self.on_start()
				else:
#					if "HangupBridgeState" in str(self) and evt.type == "ChannelEnteredBridge":
#						import pdb;pdb.set_trace()
					await self.dispatch(evt)
			except StopAsyncIteration:
				log.debug("StopRun %s", self)
				return self.result


class ChannelState(_EvtHandler):
	_src = 'channel'
	def __init__(self, channel):
		self.channel = channel
		self.client = self.channel.client

	def _repr(self):
		res=super()._repr()
		res.append(("ch_state",self.channel.state))
		return res
		
	async def dispatch(self, evt):
		"""Dispatch a single event.

		By default, call ``on_EventName(evt)``.
		If the event name starts with "Channel", this prefix is stripped.
		"""
		typ = evt.type
		try:
			handler = getattr(self, 'on_'+typ)
		except AttributeError:
			log.warn("Unhandled event %s on %s", evt, self.channel)
		else:
			await handler(evt)

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
			log.info("Unhandled DTMF %s on %s", evt.digit, self.channel)
		else:
			await proc(evt)


class BridgeState(_EvtHandler):
	"""
	Basic state machine for bridges.

	This bridge raises BridgeExit when it terminates.
	"""
	_src = 'bridge'
	TYPE="mixing"
	calls = set()

	def __init__(self, bridge):
		self.bridge = bridge
		self.client = self.bridge.client

	@classmethod
	async def new(cls, client, type="mixing", **kw):
		br = await client.bridges.create(type=type)
		s = cls(br, **kw)
		client.nursery.start_soon(s.run)
		return s

	async def add(self, channel):
		await self._add_monitor(channel)
		await self.bridge.addChannel(channel=channel.id)
		
	async def added(self, channel):
		pass

	async def remove(self, channel):
		await self.bridge.removeChannel(channel=channel.id)
		
	async def dial(self, State=ChannelState, **kw):
		"""
		Originate a call, add the called channel to this bridge.
		"""
		ch_id = self.client.generate_id()
		log.debug("DIAL %s",kw.get('endpoint', 'unknown'))
		ch = await self.client.channels.originate(channelId=ch_id, app=self.client._app, appArgs=["dialed", kw.get('endpoint', 'unknown')], **kw)
		self.calls.add(ch)
		log.debug("DIAL DONE %s",ch)
		await self._add_monitor(ch)

		s = State(ch)
		self.client.nursery.start_soon(s.run)
		return s

	async def on_StasisStart(self, evt):
		ch = evt.channel
		await self.bridge.addChannel(channel=ch.id)
#		if evt.channel not in self.bridge.channels:
#			await self.bridge.addChannel(channel=[evt.channel.id])

	async def on_ChannelConnectedLine(self, evt):
		"""Default: call .on_connected()"""
		ch = evt.channel
		if ch not in self.channels:
			await self.add(ch)
		await self.on_connected(ch)

	async def on_connected(self, channel):
		"""Called when a channel is picked up.
		Default: answer all channels that are still in RING
		"""
		for ch in self.bridge.channels:
			if ch.state == "Ring":
				await ch.answer()

	async def dispatch(self, evt):
		"""Dispatch a single event.

		By default, call ``on_EventName(evt)``.
		If the event name starts with "Bridge", this prefix is stripped.
		"""
		typ = evt.type
		try:
			handler = getattr(self, 'on_'+typ)
		except AttributeError:
			log.warn("Unhandled event %s on %s", evt, self.channel)
		else:
			await handler(evt)

	async def on_timeout(self):
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
		await self.added(ch)
		if ch.state == "Up":
			await self.on_connected(ch)

	async def _add_monitor(self, ch):
		if not hasattr(ch,'_bridge_evt'):
			log.debug("ENTER %d %s %s",id(ch),self,ch)
			ch._bridge_evt = ch.on_event("*", self._chan_evt)
			log.debug("ENTERED %s",ch._bridge_evt)

	async def _chan_evt(self, evt):
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
		await self._chan_dead(evt)

	async def on_ChannelHangupRequest(self, evt):
		try:
			await evt.channel.hangup()
		except Exception as exc:
			log.warning("Hangup %s: %s", evt.channel, exc)
		await self._chan_dead(evt)

	async def on_ChannelLeftBridge(self, evt):
		await self._chan_dead(evt)
	
	async def on_channel_end(self, ch, evt=None):
		try:
			self.calls.remove(ch)
		except KeyError:
			pass

	async def _chan_dead(self, evt):
		ch = evt.channel
		if not hasattr(ch, '_bridge_evt'):
			return
		log.debug("LEAVE %s %s",self,ch)
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

	async def teardown(self, skip_ch=None, hangup_cause="normal"):
		"""removes all channels from the bridge"""
		if self._in_shutdown:
			return
		self._in_shutdown = True

		log.info("TEARDOWN %s %s",self,self.bridge.channels)
		for ch in list(self.bridge.channels)+list(self.calls):
			if hangup_cause:
				try:
					await ch.hangup(reason=hangup_cause)
				except Exception as exc:
					log.info("%s gone: %s", ch, exc)
			if ch is skip_ch:
				continue
			try:
				await self.bridge.removeChannel(channel=ch.id)
			except Exception as exc:
				log.info("%s detached: %s", ch, exc)
				
		await self.bridge.destroy()


class HangupBridgeState(BridgeState):
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

	async def run(self):
		try:
			return await super().run()
		except BridgeExit:
			pass

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
					log.info("Channel %s gone: %s", self.channel, exc)

class OutgoingChannelState(ToplevelChannelState):
	"""A channel state machine that waits for an initial StasisStart event before proceeding"""
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		async for evt in self.channel:
			if evt.type != "StatisStart":
				raise StateError(evt)
			break
		task_status.started()
		await super().run()

