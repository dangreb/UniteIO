UniteIO
=======

UniteIO runs multiple independent asyncio applications inside one Python
process. Each concrete application class has one singleton instance, one
dedicated thread, and one event loop. Applications can share ordinary Python
state and a process-wide executor without combining all asynchronous work on a
single loop.

Why free-threaded Python?
-------------------------

On a traditional CPython build, separate loop threads provide scheduling and
failure isolation, but the GIL still serializes execution of Python bytecode.
On a free-threaded build, callbacks running on different UniteIO loop threads
can execute Python code in parallel when the runtime and invoked libraries
permit it.

For example, one application can consume Binance trade events while another
consumes ticker events. A burst of processing on the trade loop does not occupy
the ticker loop's event-loop thread. Both applications remain in the same
runtime, so they can deliberately share caches, configuration, metrics, and
other state.

Independent loops do not make shared state automatically thread-safe. Protect
mutable objects with locks or another thread-safe coordination mechanism, just
as you would with any multithreaded Python program.

Start with the quick-start guide for a two-loop streaming example, then use the
API reference for the complete class and method documentation.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   quickstart
   api

Indices
-------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
