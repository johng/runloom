"""asyncio implementation of the workflow benchmarks (single OS thread).

Idioms: asyncio.open_connection streams, asyncio.Queue for the pool and
pipeline, asyncio.gather for fan-out, asyncio.sleep for timers.  CPU work
runs inline on the loop thread -- that is the honest asyncio shape; using
run_in_executor would be measuring a thread pool, not asyncio.
"""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def _rst_close(w):
    sk = w.get_extra_info("socket")
    if sk is not None:
        try:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, C.LINGER_RST)
        except OSError:
            pass
    w.close()


async def _connect_all(n, host, port):
    """Establish all N connections before the timed window."""
    conns = await asyncio.gather(
        *(asyncio.open_connection(host, C.port_for(port, i)) for i in range(n)))
    C.mark_start()
    return conns


async def fanout_io(n, host, port):
    async def client(r, w):
        try:
            for _ in range(C.FANOUT_K):
                w.write(C.REQ)
                await w.drain()
                await r.readexactly(C.REQ_LEN)
        finally:
            _rst_close(w)
    conns = await _connect_all(n, host, port)
    await asyncio.gather(*(client(r, w) for r, w in conns))
    return n * C.FANOUT_K


async def worker_pool(n):
    per = n // C.PRODUCERS
    jobs = asyncio.Queue(C.QUEUE_CAP)
    results = asyncio.Queue(C.QUEUE_CAP)

    async def producer():
        for i in range(per):
            await jobs.put(i)

    async def worker():
        while True:
            j = await jobs.get()
            if j is None:
                return
            await results.put(C.hash_job())

    async def collector(total):
        for _ in range(total):
            await results.get()

    total = per * C.PRODUCERS
    coll = asyncio.create_task(collector(total))
    workers = [asyncio.create_task(worker()) for _ in range(C.WORKERS)]
    await asyncio.gather(*(producer() for _ in range(C.PRODUCERS)))
    for _ in range(C.WORKERS):
        await jobs.put(None)
    await asyncio.gather(*workers)
    await coll
    return total


async def pipeline(n):
    stages = 4
    qs = [asyncio.Queue(C.PIPELINE_CAP) for _ in range(stages + 1)]

    async def source():
        for i in range(n):
            await qs[0].put(C.pipeline_seed(i))
        await qs[0].put(None)

    async def stage(i):
        while True:
            item = await qs[i].get()
            if item is None:
                await qs[i + 1].put(None)
                return
            await qs[i + 1].put(C.pipeline_stage(i + 1, item))

    async def sink():
        cnt = 0
        while True:
            item = await qs[stages].get()
            if item is None:
                return cnt
            cnt += 1

    tasks = [asyncio.create_task(source())] + \
            [asyncio.create_task(stage(i)) for i in range(stages)]
    cnt = await sink()
    await asyncio.gather(*tasks)
    return cnt


async def cpu_parallel(n):
    async def task():
        C.cpu_chain()
    await asyncio.gather(*(task() for _ in range(n)))
    return n * C.CPU_ITER


async def spawn_churn(n):
    async def task():
        C.lcg(1, C.CHURN_ITER)
    await asyncio.gather(*(task() for _ in range(n)))
    return n


async def sleepers(n):
    async def task():
        for _ in range(C.SLEEP_K):
            await asyncio.sleep(C.SLEEP_S)
    await asyncio.gather(*(task() for _ in range(n)))
    return n * C.SLEEP_K


async def mixed(n, host, port):
    async def handler(r, w):
        try:
            for _ in range(C.MIXED_K):
                w.write(C.REQ)
                await w.drain()
                await r.readexactly(C.REQ_LEN)
                C.mixed_transform(C.MIXED_DOC)
                await asyncio.sleep(C.MIXED_DB_S)
                w.write(C.REQ)
                await w.drain()
                await r.readexactly(C.REQ_LEN)
        finally:
            _rst_close(w)
    conns = await _connect_all(n, host, port)
    await asyncio.gather(*(handler(r, w) for r, w in conns))
    return n * C.MIXED_K


def main():
    a = C.cli()
    host, port = C.parse_addr(a.addr)
    wl = a.workload

    async def go():
        if wl == "fanout_io":
            return await fanout_io(a.n, host, port)
        if wl == "worker_pool":
            return await worker_pool(a.n)
        if wl == "pipeline":
            return await pipeline(a.n)
        if wl == "cpu_parallel":
            return await cpu_parallel(a.n)
        if wl == "sleepers":
            return await sleepers(a.n)
        if wl == "mixed":
            return await mixed(a.n, host, port)
        if wl == "spawn_churn":
            return await spawn_churn(a.n)
        raise SystemExit("unknown workload " + wl)

    with C.Timer() as t:
        ops = asyncio.run(go())
    C.emit("asyncio", wl, a.n, ops, t.seconds, workers=1)


if __name__ == "__main__":
    main()
