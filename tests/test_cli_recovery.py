import httpx2

from student_agent.cli import _transient_connection_error


def test_nested_transport_failure_is_retryable():
    failure = ExceptionGroup("MCP stream", [httpx2.ReadError("connection closed")])
    assert _transient_connection_error(failure)


def test_programming_error_is_not_retryable():
    assert not _transient_connection_error(ValueError("invalid case"))
