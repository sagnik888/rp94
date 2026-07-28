"""Pure-Python test fallback for Numba's decorator."""


def njit(function=None, *args, **kwargs):
    if callable(function):
        return function

    def decorator(target):
        return target

    return decorator
