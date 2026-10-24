import socket

from trajectree.runner.sockets import backend_socket


def bind(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    return s


def test_backend_socket_detects_socket(settings, tmp_path):
    d = tmp_path / "sockets"
    d.mkdir()
    s = settings.model_copy(update={"sockets_dir": d})
    with bind(d / "lmstudio.sock"):
        assert backend_socket(s, "lmstudio") == d / "lmstudio.sock"
    (d / "plain.sock").write_text("not a socket")
    assert backend_socket(s, "plain") is None
    assert backend_socket(s, "missing") is None
    assert backend_socket(settings.model_copy(update={"sockets_dir": None}), "lmstudio") is None
