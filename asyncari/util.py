#

"""
Helper state machines
"""

import anyio
import math
import inspect
from asks.errors import BadStatus

__all__ = [
        "NumberError", "NumberLengthError", "NumberTooShortError", "NumberTooLongError", "NumberTimeoutError", "TotalTimeoutError", "DigitTimeoutError", 
        "mayNotExist",
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



