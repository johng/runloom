/*
 * sched_wake_credit.c -- GenMC oracle for WAKE-CREDIT CONSERVATION in runloom's
 * park_safe/wake_safe protocol, in REAL C (pthreads + C11 atomics) under RC11.
 *
 * WHY THIS EXISTS.  sched_parkwake.c proves the two SAFETY properties of the
 * Dekker handshake -- no lost wake, and enqueued-at-most-once.  Both held, and
 * both kept holding, while a real permanent hang shipped (2026-09-05, the aio
 * echo-connection strand).  The bug lived in the property NEITHER model states:
 *
 *   runloom_sched_wake_safe CREDITS g->wake_pending unconditionally, BEFORE its
 *   parked_safe CAS -- that bump is what closes the wake-before-park race.  But
 *   only runloom_sched_park_safe CONSUMES a credit.  So a credit produced for a
 *   wait episode the fiber never actually parked for is not lost, it is WORSE
 *   than lost: it survives, and silently satisfies that fiber's NEXT, unrelated
 *   park_safe, which returns without ever parking.  The next waiter then resumes
 *   on a condition that is not true yet.
 *
 * sched_parkwake.c cannot see this: it runs TWO wakers against ONE parker, so
 * wake_pending legitimately ends nonzero and conservation is not even stateable.
 * This harness models ONE wake against ONE wait EPISODE and asserts the credit
 * is consumed by the episode that caused it.
 *
 * THE PROTOCOL MODELLED is the blocking-offload wait (runloom_blockpool.c
 * runloom_blocking_call): a worker OS thread publishes `done` (RELEASE) and then
 * calls wake_safe exactly once; the fiber waits for `done`.  Note the ORDER --
 * done is visible BEFORE the wake is credited, which is precisely what lets a
 * `while (!done) park_safe();` waiter leave without parking.
 *
 * SPEC:
 *   (a) CREDIT CONSERVATION.  When the waiter has returned and the worker's
 *       wake_safe has completed, wake_pending == 0: the one credit produced was
 *       consumed by this episode and cannot corrupt a later park.
 *   (b) NO LOST WAKE (restated here so a fix for (a) cannot buy it by dropping
 *       wakes): the waiter never yields with nothing able to resume it.
 *
 * THE FIX MODELLED: park FIRST, then re-test (do/while), so the waiter cannot
 * skip the park on an already-visible `done`.  It cannot hang -- the worker's
 * wake_safe is unconditional, so a park committed after `done` is still resumed.
 *
 * Negative control (must FIND the leak = assert fails):
 *   -DBUG_WAITER_SKIPS_PARK : the waiter uses `while (!done) park_safe();` (test
 *                             first).  When it observes done before parking it
 *                             returns having consumed nothing and the credit
 *                             leaks -- the shipped bug, byte-for-byte.
 */
#include <pthread.h>
#include <stdatomic.h>
#include <assert.h>

static atomic_int parked_safe;
static atomic_int wake_pending;
static atomic_int job_done;
static atomic_int enqueued;    /* waker routed g to the wake_list */
static atomic_int yielded;     /* waiter committed to runloom_coro_yield */
static atomic_int waker_done;  /* the waker thread has finished wake_safe */

/* ---- runloom_sched_wake_safe(g), race-critical core (orders pinned to source) ---- */
static void wake_safe(void)
{
    /* src: bump FIRST so the parker's post-store recheck observes our arrival. */
    atomic_fetch_add_explicit(&wake_pending, 1, memory_order_acq_rel);
    /* src: SC fence -- StoreLoad between the bump and the CAS-load below. */
    atomic_thread_fence(memory_order_seq_cst);
    int expected = 1;
    if (atomic_compare_exchange_strong_explicit(&parked_safe, &expected, 0,
            memory_order_acq_rel, memory_order_acquire)) {
        atomic_fetch_add_explicit(&enqueued, 1, memory_order_release);
    }
}

/* ---- runloom_sched_park_safe(), race-critical core ----
 * Returns 1 if it COMMITTED to a park (reached runloom_coro_yield), else 0.
 * Every return path consumes exactly one credit, which is the accounting this
 * harness is about:
 *   - early-out / abort: decrement here;
 *   - committed park: the claimer enqueues us and the conditional decrement on
 *     resume (modelled in waiter(), after the enqueue is observed) eats it. */
static int park_safe(void)
{
    /* src: early-out if a wake is already pending. */
    if (atomic_load_explicit(&wake_pending, memory_order_acquire) > 0) {
        atomic_fetch_sub_explicit(&wake_pending, 1, memory_order_acq_rel);
        return 0;                                  /* did NOT park */
    }
    /* src: commit -- parked_safe = 1, RELEASE. */
    atomic_store_explicit(&parked_safe, 1, memory_order_release);
    /* src: SC fence -- StoreLoad before the recheck. */
    atomic_thread_fence(memory_order_seq_cst);
    /* src: recheck wake_pending after the store. */
    if (atomic_load_explicit(&wake_pending, memory_order_acquire) > 0) {
        int expected = 1;
        if (atomic_compare_exchange_strong_explicit(&parked_safe, &expected, 0,
                memory_order_acq_rel, memory_order_acquire)) {
            atomic_fetch_sub_explicit(&wake_pending, 1, memory_order_acq_rel);
            return 0;                              /* aborted the park */
        }
        /* Lost the CAS: a waker claimed us and enqueued g -> fall through. */
    }
    atomic_store_explicit(&yielded, 1, memory_order_release);
    return 1;                                      /* parked */
}

/* ---- the worker OS thread: run the job, publish done, wake exactly once ---- */
static void *worker(void *_)
{
    (void)_;
    /* src: __atomic_store_n(&job->done, DONE, RELEASE) -- BEFORE the wake. */
    atomic_store_explicit(&job_done, 1, memory_order_release);
    wake_safe();
    atomic_store_explicit(&waker_done, 1, memory_order_release);
    return 0;
}

/* ---- the waiting fiber: runloom_blocking_call's wait loop ---- */
static void *waiter(void *_)
{
    (void)_;
#ifdef BUG_WAITER_SKIPS_PARK
    /* BUG (the shipped code): TEST FIRST.  The worker publishes done before it
     * credits the wake, so observing done here returns with the credit stranded
     * on this g -- nothing in this episode ever consumes it. */
    while (!atomic_load_explicit(&job_done, memory_order_acquire)) {
        if (park_safe()) {
            /* parked: wait to be claimed, then eat the delivering credit */
            while (atomic_load_explicit(&enqueued, memory_order_acquire) == 0) { }
            if (atomic_load_explicit(&wake_pending, memory_order_acquire) > 0)
                atomic_fetch_sub_explicit(&wake_pending, 1, memory_order_acq_rel);
        }
    }
#else
    /* FIXED: PARK FIRST, then re-test.  The park cannot be skipped, so the one
     * credit this episode produces is always consumed by this episode. */
    do {
        if (park_safe()) {
            /* parked: GenMC turns the spin into an assume -- we are resumed only
             * once a waker has claimed us (that claimer is the one that bumped),
             * then park_safe's conditional decrement on resume eats the credit. */
            while (atomic_load_explicit(&enqueued, memory_order_acquire) == 0) { }
            if (atomic_load_explicit(&wake_pending, memory_order_acquire) > 0)
                atomic_fetch_sub_explicit(&wake_pending, 1, memory_order_acq_rel);
        }
    } while (!atomic_load_explicit(&job_done, memory_order_acquire));
#endif
    return 0;
}

int main(void)
{
    atomic_init(&parked_safe, 0);
    atomic_init(&wake_pending, 0);
    atomic_init(&job_done, 0);
    atomic_init(&enqueued, 0);
    atomic_init(&yielded, 0);
    atomic_init(&waker_done, 0);

    pthread_t tw, tf;
    pthread_create(&tw, 0, worker, 0);
    pthread_create(&tf, 0, waiter, 0);
    pthread_join(tw, 0);
    pthread_join(tf, 0);

    /* (b) NO LOST WAKE: a committed park must have been routed to the wake_list. */
    assert(!(atomic_load(&yielded) && atomic_load(&enqueued) == 0));
    /* (a) CREDIT CONSERVATION: the episode consumed the credit it caused, so no
     *     stray +1 survives to satisfy this fiber's NEXT, unrelated park_safe. */
    assert(atomic_load(&wake_pending) == 0);
    return 0;
}
