import unittest

from indigo.models import Node
from indigo.models.errors import UniqueException

from nose.tools import raises

class NodeTest(unittest.TestCase):

    def test_create(self):
        Node.create(name="test", address="127.0.0.1")
        node = Node.find("test")
        assert node.name == "test"
        assert node.address == '127.0.0.1'
        assert node.status == 'UP'

    @raises(UniqueException)
    def test_create_fail(self):
        Node.create(name="test_fail", address="127.0.0.1")
        Node.create(name="test_fail", address="127.0.0.1")

    def test_setstatus(self):
        Node.create(name="status_test", address="127.0.0.2")
        node = Node.find("status_test")
        assert node.status == "UP"

        node.status_down()
        node = Node.find("status_test")
        assert node.status == "DOWN"