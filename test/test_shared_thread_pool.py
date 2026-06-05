import asyncio

import pytest

from uio import UniteIO, UIOPool



class AsynAppl(UniteIO):

    async def __call__(self, sleep: int = 1, times: int = 2, *args, **kwargs):
        self.wait(
            self.submit(self.coro, sleep) if i % 2 else self.submit(self.coro(sleep))
            for i in range(times)
        )

    async def coro(self, delay: int):
        await self.asleep(delay)
        print(f'Coroutine : {delay} : Pool : {id(self.pool)} : Loop : {id(self.loop)}')



class PoolAppl(UniteIO):

    async def __call__(self, sleep: int = 1, times: int = 2, *args, **kwargs):
        self.wait(self.submit(self.call, sleep) for _ in range(times))

    def call(self, delay: int):
        self.sleep(delay)
        print(f'Callable : {delay} : Pool : {id(self.pool)} : Loop : {id(self.loop)}')



class BothAppl(UniteIO):

    async def __call__(self, sleep: int = 1, times: int = 4, *args, **kwargs):
        self.wait(
            self.submit(self.call, sleep) if i % 2 else self.submit(self.coro(sleep))
            for i in range(times)
        )

    async def coro(self, delay: int):
        await self.asleep(delay)
        print(f'Coroutine : {delay} : Pool : {id(self.pool)} : Loop : {id(self.loop)}')

    def call(self, delay: int):
        self.sleep(delay)
        print(f'Callable : {delay} : Pool : {id(self.pool)} : Loop : {id(self.loop)}')



class TestSharedThreadPool:

    def test_multiple_loops(self):
        UIOPool(single_loop=False)
        asyn_appl = AsynAppl()
        pool_appl = PoolAppl()
        assert asyn_appl.pool is pool_appl.pool, 'Pools should be the same'
        assert asyn_appl.loop is not pool_appl.loop, 'Loops should be different'

    def test_change_active_pool(self):
        UIOPool(single_loop=False)
        with pytest.raises(ValueError) as excinfo:
            UIOPool(single_loop=True)
        assert 'Cannot change an active pool. Shutdown first.' in str(excinfo.value)

    def test_shutdown(self):
        UIOPool(single_loop=False)
        UIOPool().shutdown()

    def test_single_loop(self):
        UIOPool(single_loop=True)
        asyn_appl = AsynAppl()
        pool_appl = PoolAppl()
        assert asyn_appl.pool is pool_appl.pool, 'Pools should be the same'
        assert asyn_appl.loop is pool_appl.loop, 'Loops should be the same'
