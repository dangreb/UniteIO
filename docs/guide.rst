Threads and loops
=================

This page describes how applications, threads, and the shared executor relate,
and what UniteIO does at the boundaries between them.

One loop per application
------------------------

Every application gets its own thread and its own event loop, with or without
the GIL. Consequences:

* A callback that blocks or runs long delays only its own loop.
* Tasks, async generators, and cancellation belong to one application's loop.
  asyncio objects bound to a loop (futures, events, queues in use) must not be
  shared with another application.
* Ordinary Python objects passed to several applications are shared between
  threads. Mutable ones need the usual synchronization, such as
  :class:`threading.Lock`.

With the GIL enabled, the loops are independent but their Python code takes
turns; a busy thread releases the GIL every :func:`sys.getswitchinterval`
seconds. On a free-threaded build, they run in parallel.

The number of applications is fixed by the design of the program and does not
grow with load. Work that grows with load, such as connections or requests,
belongs in tasks inside an application. ``Runtime(max_loops=...)`` sets the
maximum number of running applications; the default is a multiple of
:func:`os.process_cpu_count`, which honours ``PYTHON_CPU_COUNT`` and
``-X cpu_count``. ``max_loops=None`` removes the limit.

Crossing threads with ``submit``
--------------------------------

asyncio is not thread-safe: apart from
:meth:`~asyncio.loop.call_soon_threadsafe`, a loop's methods must be called from
the thread running it. :meth:`App.submit <uniteio.App.submit>` is the way in
from other threads.

=====================================  ========================================
``target``                             Runs
=====================================  ========================================
coroutine object                       as a task on the application's loop
coroutine function, ``*args``,         as a task on the application's loop
``**kwargs``
iterable of coroutine objects          as one :class:`asyncio.TaskGroup` on the
                                       loop; resolves to a dict mapping each
                                       coroutine to its result
any other callable, ``*args``,         on the runtime's shared executor
``**kwargs``
=====================================  ========================================

The task is created on the loop's own thread. ``name``, ``context`` and
``eager`` are passed to :meth:`~asyncio.loop.create_task` and are not forwarded
to the target; they are rejected with :exc:`TypeError` for executor work. To
pass arguments with those names to a target, use :func:`functools.partial` or
a coroutine object.

Tasks created without a name are named ``"<application>:<n>"``, so logs and
tracebacks show which application a task belongs to.

Waiting for results
~~~~~~~~~~~~~~~~~~~

``submit`` returns a :class:`concurrent.futures.Future`:

* From an ordinary thread, block on it with ``result(timeout=...)``.
* From another application's loop, await it with
  :func:`asyncio.wrap_future`::

     reply = await asyncio.wrap_future(other.submit(other.ask, "ping"))

* From the application's own loop, await the coroutine directly. Calling
  ``result()`` there would wait for the loop that is blocked waiting, so it
  raises :exc:`RuntimeError` instead.

Cancelling the future cancels the task.

Calls from the wrong thread
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Application loops raise :exc:`RuntimeError` when
:meth:`~asyncio.loop.call_soon`, :meth:`~asyncio.loop.call_later`,
:meth:`~asyncio.loop.call_at` or :meth:`~asyncio.loop.create_task` are called
from another thread. asyncio itself only checks this in debug mode. On the
application's thread, the loop behaves as usual.

Errors
------

* If the loop cannot be created or configured, :meth:`Runtime.start
  <uniteio.Runtime.start>` raises the error and nothing is registered.
* If ``__call__`` raises, the loop is already running. The error goes to
  :meth:`App.exception_handler <uniteio.App.exception_handler>` and the
  application stays alive.
* ``exception_handler`` also receives the loop's other unhandled errors:
  failing callbacks, protocol callbacks, and task exceptions that were never
  retrieved. The default delegates to asyncio's standard reporting; override it
  to route errors elsewhere.
* Errors from submitted work are delivered through its future. If nobody reads
  the future, the error is not reported.

Stopping
--------

:meth:`App.stop <uniteio.App.stop>` ends the application's loop: pending tasks
are cancelled, async generators are closed, and the loop is closed. Futures of
submitted work that had not finished are cancelled, and later ``submit`` calls
raise :exc:`RuntimeError`. Called from the application's own thread, ``stop``
only requests the stop and returns ``False``.

:meth:`Runtime.close <uniteio.Runtime.close>` stops every application
concurrently, waits for their threads, then shuts the shared executor down. It
cannot be called from an application thread. Closing a loop never shuts the
shared executor down.

:meth:`Runtime.default <uniteio.Runtime.default>` returns a process-wide
runtime, created on first use and again after it is closed.
