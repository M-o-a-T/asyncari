"""
Basic state machine for ARI channels.

The principle is very simple: On entering a state, :meth:`State.run` is
called. Exiting the state passes control back to the caller. If the channel
hangs up, a :class:`ChannelExit` exception is raised.
"""

import math
import trio
import inspect
import functools

from .model import ChannelExit, BridgeExit, EventTimeout, StateError
from async_generator import asynccontextmanager
from concurrent.futures import CancelledError

import logging
log = logging.getLogger(__name__)

__all__ = ["ToplevelChannelState", "ChannelState", "BridgeState", "HangupBridgeState", "OutgoingChannelState",
           "DTMFHandler", "EvtHandler", "as_task"]

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

class _ResultEvent:
	type = "_result"
	def __init__(self,res):
		self.res = res

# Time for a stupid helper
def _count(it):
	n = 0
	for _ in it:
		n += 1
	return n

class _ErrorEvent:
	type = "_error"
	def __init__(self,exc):
		self.exc = exc

def as_task(proc):
	@functools.wraps(proc)
	async def worker(self, *a, **kw):
		self.nursery.start_soon(functools.partial(proc, self, *a, **kw), name=proc.__name__)
	assert inspect.iscoroutinefunction(proc)
	return worker

class BaseEvtHandler:
	"""Our generic event handler.

	Event handlers can be stacked. Events will be processed by the top-most
	handler; an event percolates down if it isn't processed.

	Events get queued by calling :meth:`handle`. The handler's main loop
	repeatedly calls :meth:`get_event` to fetch the next event, and
	processes it. If the event handler either does not exist or explicitly
	returns `False`, it is relegated to the next-upper layer, or printed as
	a warning (for the bottom event handler).

	Hangups and other "terminal" events should always be processed by the
	outermost event handler.

	A handler is activated by entering its async context. It is terminated
	by calling :meth:`done`, usually triggered by an event.

	By default, specific events are processed by calling ``on_EVENTNAME``,
	though your runner is free to override that.

	To start an event handler, you typically use it as a context manager.
	Alternately, you can call its :meth:`start_task` method. In either case,
	the actual state machine will run in the background.

	Do not instantiate a ``BaseEvtHandler`` directly. Always use or
	subclass :class:`EvtHandler`, :class:`ChannelState` or
	:class:`BridgeState`.
	"""
	# Internally, start_task starts a separate task that enters this state machine's context.
	# Entering the context starts _run_with_nursery, which creates the
	# loop's nursery and then executes .run, which loops over incoming
	# events and processes them.
	# 
	# Calling .done cancels the nursery's context, thus terminates everything that's internal.
	# Awaiting the handler itself waits for the internal loop to end.

	# Main client, for Asterisk ARI calls
	client = None

	# The event handler leeching off us
	_sub = None

	# The nursery used to start our main loop
	_base_nursery = None

	# The nursery within our main loop
	_nursery = None

	# Event signalling that our main loop is done
	_done = None

	# Our event channel
	_send = None
	_recv = None

	# If this is a model-based toplevel handler, this is the name of the attribute it's based on
	_src = None

	# event for maybe-starting a new task
	_proc_check = None

	# Lock to prevent parallel runs of get_event
	_proc_lock = None

	# Number of tasks working the queue
	_n_proc = 0

	def __init__(self, client, nursery=None):
		self.client = client
		self._base_nursery = nursery or client.nursery

	async def start_task(self):
		"""This is a shortcut for running this object's async context
		manager / event loop in a separate task."""
		await self._base_nursery.start(self._run_ctx, name="start_task "+self.ref_id)

	async def _run_ctx(self, task_status=trio.TASK_STATUS_IGNORED):
		async with self:
			task_status.started()
			await self._done.wait()

	async def __aenter__(self):
		"""
		Context manager to run this state machine's "run" method / main loop.
		"""
		await self._base_nursery.start(self._run_with_nursery, name="run "+repr(self))
		return self

	@property
	def nursery(self):
		"""the nursery to use"""
		if self._nursery is None:
			return self._base_nursery
		else:
			return self._nursery

	async def _run_with_nursery(self, *, task_status=trio.TASK_STATUS_IGNORED):
		try:
			assert self._done is None, self._done
			self._done = trio.Event()
			self._send, self._recv = trio.open_memory_channel(20)

			async with trio.open_nursery() as nursery:
				self._nursery = nursery
				await self.run(task_status=task_status)
		finally:
			self._nursery = None
			self._done.set()
			self._done = None
			await self._send.aclose()

			# Any unprocessed events get relegated to the parent
			while True:
				try:
					evt = self._recv.receive_nowait()
				except trio.EndOfChannel:
					break
				else:
					await self._handle_prev(evt)


	def done(self):
		"""Signal that this event handler has finished.

		This call cancels the main loop, if any, as well as the loop of any
		sub-event handlers which might be running.
		"""
		if self._nursery is not None:
			self._nursery.cancel_scope.cancel()


	async def __aexit__(self, *tb):
		self.done()

		if self._done is not None:
			await self._done.wait()


	def done_sub(self):
		"""Terminate my sub-handler, assuming one exists.

		Returns True if there was a sub-handler to cancel, False
		otherwise.
		"""
		if not self._sub:
			return False
		self._sub.done()
		self._sub = None
		return True


	async def handle(self, evt):
		"""Dispatch a single event to this handler.

		* Feed the event to the current sub-handler, if any.
		* If the event is handled, return True.
		* Otherwise, call ``self.on_EventName(evt)``. If that handler
		  explicitly returns False, return that, else return True.
		"""
		if self._sub is not None:
			await self._sub.handle(evt)
		else:
			await self._handle_here(evt)

	async def _handle_here(self, evt):
		try:
			await self._send.send(evt)
		except trio.ClosedResourceError:
			log.info("Unhandled event %s on %s (closed)", evt.type, self)


	async def _dispatch(self, evt):
		typ = evt.type
		try:
			handler = getattr(self, 'on_'+typ)
		except AttributeError:
			await self._handle_prev(evt)
			return
		res = handler(evt)
		if inspect.iscoroutine(res):
			res = await res

		if res is not False and not res:
			res = True
		return res

	async def _handle_prev(self, evt):
		log.info("Unhandled event %s on %s", evt.type, self)
		return False

	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		"""
		Process my events.

		Override+call this e.g. for overall timeouts::

			async def run(self):
				with trio.fail_after(30):
					await super().run()

		You must call :meth:`_handle_prev` on events you don't recognize.

		This method creates a runner task that do the actual event processing.
		A new runner is started if processing an event takes longer than 0.1 seconds.

		Do not replace this method. Do not call it directly.
		"""
		log.debug("StartRun %s", self)
		task_status.started()
		await self.on_start()

		self._proc_lock = trio.Lock()
		while True:
			if self._n_proc == 0:
				await self.nursery.start(self._process, name="Worker "+self.ref_id)
			self._proc_check = trio.Event()
			await trio.sleep(0.1)
			await self._proc_check.wait()

	async def _process(self, task_status=trio.TASK_STATUS_IGNORED):
		task_status.started()
		try:
			while True:
				self._n_proc += 1
				try:
					async with self._proc_lock:
						evt = await self.get_event()
				except StopAsyncIteration:
					return
				finally:
					self._n_proc -= 1
				if self._n_proc == 0:
					self._proc_check.set()

				# Any unhandled event is relegated to the parent
				try:
					success = await self._dispatch(evt)
				except BaseException as exc:
					await self._handle_prev(evt)
					raise
				else:
					if success:
						await self._handle_prev(evt)

		finally:
			log.debug("StopRun %s", self)

	async def get_event(self):
		"""
		Get the next event from this handler's queue.
		Supersede this e.g. for per-event timeouts::

			class TimeoutEvent:
				type = "MyTimeout"

			async def get_event():
				with trio.move_on_after(30):
					return await super().get_event()
				return TimeoutEvent()

			async on_MyTimeout(self, evt):
				self.done(None)

		Raises StopAsyncIteration when no more events will arrive.
		"""
		try:
			evt = await self._recv.receive()
		except trio.EndOfChannel:
			raise StopAsyncIteration
		log.debug("Event:%s %s", self, evt)
		return evt

	def _repr(self):
		"""List of attribute+value pairs to include in ``repr``."""
		res = []
		if self._src:
			res.append((self._src, getattr(self,self._src)))
		return res

	@property
	def ref(self):
		if self._src is None:
			return None
		return getattr(self, self._src)

	@property
	def ref_id(self):
		r = self.ref
		if r is None:
			return '?'
		return r.id

	def __repr__(self):
		return "<%s: %s>" % (self.__class__.__name__, ','.join("%s=%s"%(a,b) for a,b in self._repr()))

	async def on_start(self):
		"""Called when the state machine starts up (initial pseudo event).
		Defaults to doing nothing.
		"""
		pass

	async def on_result(self, res):
		"""Called when a sub-handler's state machine returns a value.
		The default is to do nothing.
		"""
		pass

	async def on_error(self, exc):
		"""Called when a sub-handler's state macheine raises an error.

		The default is to re-raise the error.
		"""
		raise exc

	def on__result(self, evt):
		"""Dispatcher-internal method. Please ignore."""
		return self.on_result(evt.res)

	def on__error(self, evt):
		"""Dispatcher-internal method. Please ignore."""
		return self.on_error(evt.exc)

	def __await__(self):
		"""Wait for the run task to terminate and return its result."""
		yield from self._done.wait().__await__()


class _EvtHandler(BaseEvtHandler):
	"""
	common methods for AsyncEvtHandler and SyncEvtHandler
	"""
	# The event handler we're leeching off of
	_prev = None

	# Our main loop's result
	_result = None

	def __init__(self, prev):
		self._prev = prev
		super().__init__(prev.client, nursery=prev.nursery)

	async def _handle_prev(self, evt):
		await self._prev._handle_here(evt)
		return True

	async def _run_with_nursery(self, **kw):
		# the event handler stack doesn't allow branches
		if self._prev._sub is not None:
			raise RuntimeError("Our parent already has a sub-handler")
		self._prev._sub = self

		try:
			await super()._run_with_nursery(**kw)

		finally:
			if self._prev._sub is not self:
				raise RuntimeError("Problem nesting event handlers")
			self._prev._sub = None

	def __await__(self):
		# alias "await Handler()" to "await Handler()._await()"
		return self._await().__await__()

	def done(self, result=None):
		"""Signal that this event handler has finished with this result.
		"""
		if result is not None:
			self._result = result
		super().done()

	async def _await(self):
		raise RuntimeError("Use a subclass.")

class AsyncEvtHandler(_EvtHandler):
	"""
	This event handler operates asynchronously, i.e. you start it
	off and get its result in an event::

		class MenuOne(AsyncEvtHandler):
			pass  # do whatever it takes to handle this submenu
			# Somewhere in there you'll call "self.done(RESULT)"

		async def on_dtmf_1(self evt):
			await MenuOne(self)  # this returns (almost) immediately

		async def on_result(self, res):
			pass  # do whatever you want with RESULT

		async def on_error(self, err):
			raise err  # do whatever you want with the error

	Alternately, use :class:`SyncEvtHandler` in a separate task.

	"""
	async def _run_with_nursery(self, *, task_status=trio.TASK_STATUS_IGNORED):
		try:
			await super()._run_with_nursery(task_status=task_status)
		except Exception as exc:
			await self._handle_prev(_ErrorEvent(exc))
		except trio.Cancelled:
			if self._done.is_set():
				await self._handle_prev(_ResultEvent(self._result))
			else:
				await self._handle_prev(_ErrorEvent(CancelledError()))
			raise
		except BaseException:
			await self._handle_prev(_ErrorEvent(CancelledError()))
			raise
		else:
			await self._handle_prev(_ResultEvent(self._result))

	async def _await(self):
		await self._start_task()


class SyncEvtHandler(_EvtHandler):
	"""
	This event handler operates synchronously, i.e. you can simply run it
	and get its result::

		class MenuOne(SyncEvtHandler):
			pass  # do whatever it takes to handle this submenu
			# Somewhere in there you'll call "self.done(RESULT)"

		@as_task
		async def on_digit_1(self evt):
			try:
				res = await MenuOne(self)
			except Exception as err:
				raise  # do whatever you want with the error
			else:
				pass  # do whatever you want with RESULT

	You **must** decorate your handler with :func:`as_task` or otherwise
	delegate this call to another task. If you don't, event handling **will**
	deadlock. You'll also get an error message that your event handler
	takes too long.

	Alternately, use :class:`AsyncEvtHandler` and `on_result`.
	"""

	async def _await(self):
		"""This does not use context management, because we want to get errors."""
		await self._run_with_nursery()

		if isinstance(self._result, Exception):
			raise self._result
		return self._result


class DTMFHandler:
	"""A handler mix-in that dispatches DTMF tones to specific handlers.

	This is not a stand-alone class – use as a mix-in to ``EvtHandler``,
	``ChannelState``, or ``BridgeState``.
	"""

	async def on_ChannelDtmfReceived(self, evt):
		"""Dispatch DTMF events.

		Calls ``on_dtmf_{0-9,A-D,Star,Pound}`` methods. (Note capitalization.)
		If that doesn't exist and a letter is dialled, call ``on_dtmf_letter``.
		If that doesn't exist and a digit is dialled, call ``on_dtmf_digit``.
		If that doesn't exist either, call ``on_dtmf``.
		If that doesn't exist either, punt to calling state machine.
		"""

		digit = evt.digit
		if digit == '#':
			digit = 'Pound'
		elif digit == '*':
			digit = 'Star'
		proc = getattr(self,'on_dtmf_'+digit, None)
		if proc is None and digit >= '0' and digit <= '9':
			proc = getattr(self,'on_dtmf_digit', None)
		if proc is None and digit >= 'A' and digit <= 'D':
			proc = getattr(self,'on_dtmf_letter', None)
		if proc is None:
			proc = getattr(self,'on_dtmf', None)

		if proc is None:
			log.info("Unhandled DTMF %s on %s", evt.digit, self)
			return False
		else:
			p = proc(evt)
			if inspect.iscoroutine(p):
				p = await p
			return p

class _ThingEvtHandler(BaseEvtHandler):
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		if self._nursery is None:
			raise RuntimeError("I do not have a nursery. Use 'async with' or 'start_task'.")
		handler = self.ref.on_event("*", self.handle)
		try:
			await super().run(task_status=task_status)
		finally:
			handler.close()


class ChannelState(_ThingEvtHandler):
	"""This is the generic state machine for a single channel."""
	_src = 'channel'
	def __init__(self, channel):
		self.channel = channel
		super().__init__(channel.client)

	def _repr(self):
		res=super()._repr()
		res.append(("ch_state",self.channel.state))
		return res

	def on_StasisEnd(self, evt):
		self.done()


class BridgeState(_ThingEvtHandler):
	"""
	This is the generic state machine for a bridge.

	The bridge is always auto-destroyed when its handler ends.
	"""
	_src = 'bridge'
	TYPE="mixing"
	calls = set()
	bridge = None

	def __init__(self, bridge, **kw):
		self.bridge = bridge
		super().__init__(bridge.client, **kw)

	@classmethod
	def new(cls, client, type="mixing", **kw):
		"""
		Create a new bridge with this state machine.

		Always use as `async with …`.
		"""
		s = object.__new__(cls)
		s.client = client
		s._bridge_args = dict(type=type, bridgeId=client.generate_id("B"))
		return s

	async def __aenter__(self):
		if self.bridge is None:
			self.__init__(await self.client.bridges.create(**self._bridge_args))
			del self._bridge_args
		return await super().__aenter__()

	async def __aexit__(self, *tb):
		await self.teardown()
		return super().__aexit__(*tb)

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
		ch_id = self.client.generate_id("C")
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
			await s.start_task()
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
		"""Hook for channel creation. Connects the channel to this bridge.
		
		Call when overriding!"""
		ch = evt.channel
		await self.bridge.addChannel(channel=ch.id)

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
		if ch.state == "Up":# and ch.prev_state != "Up":
			await self.on_connected(ch)

	async def on_ChannelLeftBridge(self, evt):
		await self._chan_dead(evt)

	async def _add_monitor(self, ch):
		"""Listen to non-bridge events on the channel"""
		if not hasattr(ch,'_bridge_evt'):
			ch._bridge_evt = ch.on_event("*", self._chan_evt)

	async def _chan_evt(self, evt):
		"""Dispatcher for forwarding a channel's events to this bridge."""
		if getattr(evt,'bridge',None) is self:
			log.debug("Dispatch hasBRIDGE:%s for %s",evt.type,self)
			return # already calling us via regular dispatch
		await self.handle(evt)

	async def on_ChannelStateChange(self, evt):
		"""calls self._chan_state_change"""
		await self._chan_state_change(evt)

	async def on_ChannelConnectedLine(self, evt):
		"""calls self._chan_state_change"""
		await self._chan_state_change(evt)

	async def on_ChannelDestroyed(self, evt):
		"""calls self._chan_dead"""
		await self._set_cause(evt)
		await self._chan_dead(evt)

	async def on_ChannelHangupRequest(self, evt):
		"""kills the channel"""
		await self._set_cause(evt)
		try:
			await evt.channel.hang_up()
		except Exception as exc:
			log.warning("Hangup %s: %s", evt.channel, exc)

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
		"""Set the hangup cause for this bridge's channels"""
		try:
			cc = evt.cause
		except AttributeError:
			pass
		else:
			cc = CAUSE_MAP.get(cc,"normal")
			for c in list(self.bridge.channels)+list(self.calls):
				await c.set_reason(cc)

	async def _chan_dead(self, evt):
		ch = evt.channel

		if not hasattr(ch, '_bridge_evt'):
			return

		# remove the listener
		ch._bridge_evt.close()
		del ch._bridge_evt

		await self.on_channel_end(ch, evt)

	async def _chan_state_change(self, evt):
		"""react to channel state change"""
		ch = evt.channel
		log.debug("StateChange %s %s", self, ch)
		if ch not in self.bridge.channels:
			return
		if ch.state == "Up":
			await self.on_connected(ch)

	async def teardown(self, hangup_reason="normal"):
		"""Removes all channels from the bridge and destroys it.

		All remaining channels are hung up.

		This method is typically called when leaving the bridge's context
		manager. If you want to keep it online, e.g. for being able to
		cleanly restart a PBX without downtime, you may override this --
		but you're then responsible for recovering state after restarting,
		and you still need to clean up bridges that are no longer needed.

		"""
		with trio.move_on_after(2) as s:
			s.shield = True
			log.info("TEARDOWN %s %s",self,self.bridge.channels)
			for ch in list(self.bridge.channels)+list(self.calls):
				try:
					await ch.hang_up(reason=hangup_reason)
				except Exception as exc:
					log.info("%s gone: %s", ch, exc)

				try:
					await self.bridge.removeChannel(channel=ch.id)
				except Exception as exc:
					log.info("%s detached: %s", ch, exc)

			await self.bridge.destroy()


class HangupBridgeState(BridgeState):
	"""A bridge controller that hangs up all channels and deletes its
	bridge as soon as there is only one active channel left.
	"""
	async def on_channel_end(self, ch, evt=None):
		await super().on_channel_end(ch, evt)
		if _count(1 for c in self.bridge.channels if c.state == 'Up') < 2:
			self.done()


class ToplevelChannelState(ChannelState):
	"""A channel state machine that unconditionally hangs up its channel on exception"""
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		"""Task for this state. Hangs up the channel on exit."""
		try:
			await super().run(task_status=task_status)
		except ChannelExit:
			pass
		except StateError:
			pass
		finally:
			with trio.fail_after(2) as s:
				s.shield = True
				await self.channel.exit_hangup()

	async def hang_up(self, reason="normal"):
		await self.channel.set_reason(reason)
		await self.channel.hang_up()

class OutgoingChannelState(ToplevelChannelState):
	"""A channel state machine that waits for an initial StasisStart event before proceeding"""
	async def run(self, task_status=trio.TASK_STATUS_IGNORED):
		async for evt in self.channel:
			if evt.type != "StatisStart":
				raise StateError(evt)
			break
		await super().run(task_status=task_status)

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
				await state.start_task()
			except BaseException:
				with trio.open_cancel_scope(shield=True):
					await ch.hangup()
				raise

	async def __aexit__(self, *exc):
		if self.state is None:
			await self.state.hang_up()
		else:
			await self.channel.hang_up()

