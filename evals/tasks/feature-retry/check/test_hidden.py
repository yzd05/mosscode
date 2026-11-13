import pytest
from retry import retry


def flaky(failures, error=ConnectionError):
    calls = []

    def call():
        calls.append(1)
        if len(calls) <= failures:
            raise error(f"failure {len(calls)}")
        return "ok"

    return call, calls


def test_succeeds_after_failures():
    call, calls = flaky(2)
    assert retry(3)(call)() == "ok"
    assert len(calls) == 3


def test_gives_up_with_the_last_error():
    call, calls = flaky(5)
    with pytest.raises(ConnectionError, match="failure 3"):
        retry(3)(call)()
    assert len(calls) == 3


def test_other_exceptions_are_not_retried():
    call, calls = flaky(1, error=KeyError)
    with pytest.raises(KeyError):
        retry(3, exceptions=(ConnectionError,))(call)()
    assert len(calls) == 1


def test_arguments_pass_through_and_metadata_is_kept():
    @retry(2)
    def add(a, b=0):
        """Add two numbers."""
        return a + b

    assert add(1, b=2) == 3
    assert add.__name__ == "add" and add.__doc__ == "Add two numbers."


def test_invalid_attempts():
    with pytest.raises(ValueError):
        retry(0)
