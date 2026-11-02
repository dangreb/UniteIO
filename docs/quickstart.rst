Quick start
===========

Installation
------------

::

   pip install uniteio

UniteIO requires Python 3.14.5 or newer. It runs on regular and free-threaded
builds. To check which one you have:

.. code-block:: console

   $ python -c "import sys, sysconfig; print('free-threaded build:', bool(sysconfig.get_config_var('Py_GIL_DISABLED'))); print('GIL enabled:', sys._is_gil_enabled())"

A free-threaded build can still run with the GIL enabled, for example when a C
extension that does not support free threading is imported. See Python's
`free-threading guide <https://docs.python.org/3/howto/free-threading-python.html>`_.

Defining an application
-----------------------

An application subclasses :class:`~uniteio.App` and implements
``async __call__``. The constructor is an ordinary constructor: it takes any
arguments and runs on the caller's thread. ``__call__`` runs later, on the
application's own loop.

The example below starts a small TCP server that upper-cases each line it
receives, and a second application that counts lines in an object shared with
the server.

.. code-block:: python

   import asyncio
   import threading

   from uniteio import App, Runtime


   class Counter:
       """Shared by both application threads, so it is protected by a lock."""

       def __init__(self):
           self._lock = threading.Lock()
           self.lines = 0

       def add(self):
           with self._lock:
               self.lines += 1


   class UpperServer(App):
       name = "upper"

       def __init__(self, counter):
           self.counter = counter
           self.ready = asyncio.Event()  # created here, used only on the app's loop

       async def handle(self, reader, writer):
           while line := await reader.readline():
               self.counter.add()
               writer.write(line.upper())
               await writer.drain()
           writer.close()
           await writer.wait_closed()

       async def __call__(self):
           self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
           self.port = self.server.sockets[0].getsockname()[1]
           self.ready.set()
           await self.server.serve_forever()


   class Client(App):
       async def ask(self, port, text):
           reader, writer = await asyncio.open_connection("127.0.0.1", port)
           writer.write(text.encode() + b"\n")
           reply = await reader.readline()
           writer.close()
           await writer.wait_closed()
           return reply.decode().strip()

       async def __call__(self):
           pass  # nothing to set up; work arrives through submit()

Starting and finding applications
---------------------------------

:meth:`Runtime.start <uniteio.Runtime.start>` starts the application on a new
thread and returns the same object once its loop is running. Leaving the
``with`` block closes the runtime: every application is stopped, then the
executor is shut down.

.. code-block:: python

   counter = Counter()

   with Runtime() as rt:
       server = rt.start(UpperServer(counter))
       client = rt.start(Client())

       server.submit(server.ready.wait).result(timeout=2)
       reply = client.submit(client.ask, server.port, "hello").result(timeout=2)

       assert reply == "HELLO"
       assert counter.lines == 1
       assert rt["upper"] is server and rt[Client] is client

The registry name comes from the ``name`` argument of ``start``, a ``name``
class attribute, or the class name, in that order. Running several instances of
one class only needs different names:

.. code-block:: python

   first = rt.start(Client(), name="client-1")
   second = rt.start(Client(), name="client-2")

Submitting work
---------------

:meth:`App.submit <uniteio.App.submit>` is how other threads hand work to an
application. Coroutines run on the application's loop; other callables run on
the runtime's shared executor. Both return a
:class:`concurrent.futures.Future`.

.. code-block:: python

   reply = client.submit(client.ask, server.port, "hi").result(timeout=2)
   total = client.submit(sum, [1, 2, 3]).result(timeout=2)   # runs on the executor

Inside an application's loop, use asyncio directly: ``await``,
:func:`asyncio.create_task`, :class:`asyncio.TaskGroup`. The
:doc:`guide` describes ``submit`` in detail.

Stopping
--------

Stop one application with :meth:`App.stop <uniteio.App.stop>`; the others keep
running. :meth:`Runtime.close <uniteio.Runtime.close>`, which the ``with`` block
calls on exit, stops all of them and then shuts the executor down.

.. code-block:: python

   client.stop(timeout=2)
   assert not client.is_alive()
