Quick start
===========

Installation
------------

Install the package into a Python 3.14.5 or newer environment::

   pip install UniteIO

Create an application
---------------------

Subclass :class:`uio.UniteIO` and implement its asynchronous entry point:

.. code-block:: python

   import asyncio

   from uio import UniteIO


   class Service(UniteIO, prefix="SVC"):
       def __init__(self, endpoint: str):
           super().__init__(endpoint=endpoint)

       async def __call__(self) -> None:
           await asyncio.Event().wait()


   service = Service(endpoint="local")
   assert service.endpoint == "local"

   result = service.submit(sum, [1, 2, 3]).result()
   assert result == 6

   service.stop()

Every concrete application class has one active instance and its own asyncio
loop. Synchronous callables submitted through ``submit`` run on the shared
:class:`uio.UIOPool`.

Shutdown
--------

Stop one application without affecting the shared executor::

   service.stop(timeout=2)

Or stop every registered application before shutting down the executor::

   from uio import UIOPool

   UIOPool().shutdown(cancel_futures=True)

