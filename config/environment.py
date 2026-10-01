"""Small, explicit environment parsers; invalid configuration fails at startup."""

import os

from django.core.exceptions import ImproperlyConfigured


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in ('true', '1'):
        return True
    if normalized in ('false', '0'):
        return False
    raise ImproperlyConfigured(f'{name} must be true/false or 1/0.')


def env_list(name, default=''):
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


def env_nonnegative_int(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ImproperlyConfigured(f'{name} must be a non-negative integer.') from None
    if value < 0:
        raise ImproperlyConfigured(f'{name} must be a non-negative integer.')
    return value
