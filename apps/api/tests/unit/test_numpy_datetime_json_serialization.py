"""Regression tests for JSON-safe NumPy datetime scalar conversion."""

import datetime
import json
import unittest

import numpy as np

from shared.utils.json_utils import make_json_safe


class NumpyDateTimeJsonSerializationTests(unittest.TestCase):
    def assert_serializes_as(self, value, expected):
        actual = make_json_safe(value)
        self.assertEqual(json.loads(json.dumps(actual)), expected)
        self.assertEqual(actual, expected)

    def test_numpy_date(self):
        self.assert_serializes_as(
            np.datetime64("2026-11-06", "D"), "2026-11-06"
        )

    def test_numpy_datetime_seconds(self):
        self.assert_serializes_as(
            np.datetime64("2026-11-06T12:34:56", "s"), "2026-11-06T12:34:56"
        )

    def test_numpy_datetime_milliseconds(self):
        self.assert_serializes_as(
            np.datetime64("2026-11-06T12:34:56.123", "ms"),
            "2026-11-06T12:34:56.123000",
        )

    def test_numpy_datetime_microseconds(self):
        self.assert_serializes_as(
            np.datetime64("2026-11-06T12:34:56.123456", "us"),
            "2026-11-06T12:34:56.123456",
        )

    def test_python_date_control(self):
        self.assert_serializes_as(datetime.date(2026, 11, 6), "2026-11-06")

    def test_python_datetime_control(self):
        self.assert_serializes_as(
            datetime.datetime(2026, 11, 6, 12, 34, 56), "2026-11-06T12:34:56"
        )

    def test_numpy_integer_control(self):
        self.assert_serializes_as(np.int64(7), 7)

    def test_numpy_boolean_control(self):
        self.assert_serializes_as(np.bool_(True), True)

    def test_numpy_float_control(self):
        self.assert_serializes_as(np.float32(0.125), 0.125)

    def test_numpy_nat_control(self):
        self.assert_serializes_as(np.datetime64("NaT", "D"), None)

    def test_numpy_nanosecond_integer_control(self):
        self.assert_serializes_as(
            np.datetime64("1970-01-01T00:00:00.000000123", "ns"), 123
        )


if __name__ == "__main__":
    unittest.main()

