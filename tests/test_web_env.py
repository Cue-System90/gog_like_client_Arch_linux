import os
import unittest
from unittest import mock

from galaxy_linux.ui import web


class WebEnvironmentTest(unittest.TestCase):
    def test_gl_sink_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEBKIT_GST_DISABLE_GL_SINK", None)
            web.prepare_environment()
            self.assertEqual(os.environ.get("WEBKIT_GST_DISABLE_GL_SINK"), "1")

    def test_user_value_wins(self):
        with mock.patch.dict(os.environ, {"WEBKIT_GST_DISABLE_GL_SINK": "0"}):
            web.prepare_environment()
            self.assertEqual(os.environ["WEBKIT_GST_DISABLE_GL_SINK"], "0")


if __name__ == "__main__":
    unittest.main()
