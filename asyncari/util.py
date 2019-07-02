#

"""
Helper state machines
"""

import anyio
import math
import inspect
from asks.errors import BadStatus

from .state import SyncEvtHandler, AsyncEvtHandler, DTMFHandler

__all__ = [
        "NumberError", "NumberLengthError", "NumberTooShortError", "NumberTooLongError", "NumberTimeoutError", "TotalTimeoutError", "DigitTimeoutError", 
        "SyncReadNumber", "AsyncReadNumber",
        "SyncPlay", "mayNotExist",
        ]

def singleton(cls):
    return cls()

class NumberError(RuntimeError):
    """Base class for things that can go wrong entering a number.
    Attributes:
        number:
            The (partial, wrong, …) number that's been dialled so far.
    """
    def __init__(self, num):
        self.number = num
class NumberLengthError(NumberError):
    pass
class NumberTooShortError(NumberLengthError):
    pass
class NumberTooLongError(NumberLengthError):
    pass
class NumberTimeoutError(NumberError):
    pass
class TotalTimeoutError(NumberTimeoutError):
    pass
class DigitTimeoutError(NumberTimeoutError):
    pass

class _ReadNumber(DTMFHandler):
    _digit_timer = None
    _digit_deadline = None
    _total_timer = None
    _total_deadline = None

    def __init__(self, prev, playback=None, timeout=60, first_digit_timeout=None, digit_timeout=10, max_len=15, min_len=5):
        if first_digit_timeout is None:
            first_digit_timeout = digit_timeout
        self.total_timeout = timeout
        self.digit_timeout = digit_timeout
        self.first_digit_timeout = first_digit_timeout
        self.min_len = min_len
        self.max_len = max_len
        self.playback = playback

        super().__init__(prev)

    async def add_digit(self, digit):
        """
        Add this digit to the current number.

        The default clears the number on '*' and returns it on '#',
        assuming that the length restrictions are obeyed.

        This method may call `await self.done` with the dialled number, update
        `self.num`, or raise an exception. A string is used to replace the
        current temporary number.
        
        This method may be a coroutine.
        """
        if digit == '*':
            self.num = ""
        elif digit == '#':
            if len(self.num) < self.min_len:
                raise NumberTooShortError(self.num)
            await self.done(self.num)
        else:
            self.num += digit
            if len(self.num) > self.max_len:
                raise NumberTooLongError(self.num)

    async def _stop_playing(self):
        if self.playback is not None:
            pb, self.playback = self.playback, None
            try:
                await pb.stop()
            except BadStatus:
                pass

    async def _digit_timer_(self, evt: anyio.abc.Event = None):
        self._digit_deadline = self.first_digit_timeout + await anyio.current_time()
        async with anyio.open_cancel_scope() as sc:
            self._digit_timer = sc
            if evt is not None:
                await evt.set()
            while True:
                delay = self._digit_deadline - await anyio.current_time()
                if delay <= 0:
                    await self._stop_playing()
                    raise DigitTimeoutError(self.num) from None
                await anyio.sleep(delay)

    async def _total_timer_(self, evt: anyio.abc.Event = None):
        self._total_deadline = self.total_timeout + await anyio.current_time()
        async with anyio.open_cancel_scope() as sc:
            self._total_timer = sc
            if evt is not None:
                await evt.set()
            while True:
                delay = self._total_deadline - await anyio.current_time()
                if delay <= 0:
                    await self._stop_playing()
                    raise NumberTimeoutError(self.num) from None
                await anyio.sleep(delay)

    async def done(self, res):
        await super().done(res)
        await self._digit_timer.cancel()
        await self._total_timer.cancel()

    async def on_start(self):
        self.num = ""
        await self.taskgroup.spawn(self._digit_timer_)
        await self.taskgroup.spawn(self._total_timer_)

    async def on_dtmf_letter(self, evt):
        """Ignore DTMF letters (A-D)."""
        pass

    async def on_dtmf(self, evt):
        await self._stop_playing()
        res = await self.add_digit(evt.digit)
        if inspect.iscoroutine(res):
            res = await res
        if isinstance(res, str):
            self.num = res
        await self.set_timeout()

    async def set_timeout(self):
        self._digit_deadline = (await anyio.current_time()) + (self.digit_timeout if self.num else self.first_digit_timeout)


class SyncReadNumber(_ReadNumber,SyncEvtHandler):
    """
    This event handler receives and returns a sequence of digits.
    The pound key terminates the sequence. The star key restarts.

    Sync version.
    """
    pass

class AsyncReadNumber(_ReadNumber,AsyncEvtHandler):
    """
    This event handler receives and returns a sequence of digits.
    The pound key terminates the sequence. The star key restarts.

    Async version.
    """
    pass

class SyncPlay(SyncEvtHandler):
	"""
	This event handler plays a sound and returns when it has finished.

	Sync version. There is no async version because you get an event with the result anyway.
	"""
	def __init__(self, prev, media):
		super().__init__(prev)
		self.media = media
		self.chan_or_bridge = self.ref
	
	async def on_start(self):
		p = await self.chan_or_bridge.play(media=self.media)
		p.on_event("PlaybackFinished", self.on_play_end)

	async def on_play_end(self, evt):
		await self.done()


from asks.errors import BadStatus
NOT_FOUND = 404

@singleton
class mayNotExist:
    def __enter__(self):
        return self
    def __exit__(self, c,e,t):
        if e is not None:
            if isinstance(e, BadStatus) and e.status_code == NOT_FOUND:
                return True



