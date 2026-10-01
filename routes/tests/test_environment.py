import os
import importlib.util
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.environment import env_bool, env_list, env_nonnegative_int
from config import settings as project_settings


def load_settings():
    spec = importlib.util.spec_from_file_location('config._settings_test', project_settings.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


class EnvironmentTests(SimpleTestCase):
    def test_debug_boolean_values(self):
        for value, expected in [('true', True), (' TRUE ', True), ('1', True),
                                ('false', False), (' FALSE ', False), ('0', False)]:
            with self.subTest(value=value), patch.dict(os.environ, {'DEBUG': value}):
                self.assertEqual(env_bool('DEBUG', True), expected)

    def test_invalid_debug_fails_explicitly(self):
        with patch.dict(os.environ, {'DEBUG': 'release'}):
            with self.assertRaises(ImproperlyConfigured):
                env_bool('DEBUG', True)

    def test_allowed_hosts(self):
        with patch.dict(os.environ, {'ALLOWED_HOSTS': ' localhost, example.com , , [::1] '}):
            self.assertEqual(env_list('ALLOWED_HOSTS'), ['localhost', 'example.com', '[::1]'])

    def test_cache_ttl_validation(self):
        for value in ('-1', 'abc', '1.5', ''):
            with self.subTest(value=value), patch.dict(os.environ, {'ROUTING_CACHE_TTL_SECONDS': value}):
                with self.assertRaises(ImproperlyConfigured):
                    env_nonnegative_int('ROUTING_CACHE_TTL_SECONDS', 86400)
        with patch.dict(os.environ, {'ROUTING_CACHE_TTL_SECONDS': '0'}):
            self.assertEqual(env_nonnegative_int('ROUTING_CACHE_TTL_SECONDS', 86400), 0)

    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(env_bool('DEBUG', True))
            self.assertEqual(env_nonnegative_int('ROUTING_CACHE_TTL_SECONDS', 86400), 86400)

    def test_settings_environment_overrides(self):
        with patch.dict(os.environ, {'DEBUG': 'false', 'SECRET_KEY': 'test-only',
                                     'ALLOWED_HOSTS': 'example.com', 'ROUTING_CACHE_TTL_SECONDS': '123'}, clear=True):
            settings = load_settings()
        self.assertFalse(settings['DEBUG'])
        self.assertEqual(settings['SECRET_KEY'], 'test-only')
        self.assertEqual(settings['ALLOWED_HOSTS'], ['example.com'])
        self.assertEqual(settings['ROUTING_CACHE_TTL_SECONDS'], 123)

    def test_non_debug_requires_secret(self):
        with patch.dict(os.environ, {'DEBUG': 'false'}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                load_settings()

    def test_non_debug_rejects_whitespace_secret(self):
        with patch.dict(os.environ, {'DEBUG': 'false', 'SECRET_KEY': '   '}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                load_settings()
