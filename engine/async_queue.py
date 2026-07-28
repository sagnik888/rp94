import threading
import queue
import time
from typing import Callable, Any
from loguru import logger

class AsyncExecutionQueue:
    """
    Bounded background worker pool for broker API calls and heavy tasks.
    Simultaneous valid signals progress independently without unbounded threads.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(AsyncExecutionQueue, cls).__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self._queue_size = 500
        self.task_queue = queue.Queue(maxsize=self._queue_size)
        self.worker_count = 4
        self.worker_threads = []
        self._accepting = False
        self._shutdown_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self.start()

    def _spawn_workers(self, count: int) -> None:
        start_index = len(self.worker_threads)
        for index in range(start_index, start_index + max(0, int(count))):
            worker = threading.Thread(
                target=self._process_queue,
                daemon=True,
                name=f"AsyncExecutionQueueWorker-{index + 1}",
            )
            worker.start()
            self.worker_threads.append(worker)

    def start(self) -> bool:
        """Start or restart the bounded worker pool when no prior workers remain."""
        with self._shutdown_lock:
            live_workers = [worker for worker in self.worker_threads if worker.is_alive()]
            if live_workers:
                with self._state_lock:
                    accepting = self._accepting
                if not accepting:
                    logger.warning(
                        "AsyncExecutionQueue restart refused while prior workers are still stopping."
                    )
                    return False
                self.worker_threads = live_workers
                missing = max(0, self.worker_count - len(live_workers))
                self._spawn_workers(missing)
                self.worker_thread = self.worker_threads[0]
                return True

            # A fresh queue makes an in-process FastAPI lifespan restart safe and
            # discards bookkeeping attached to the completed worker generation.
            self.task_queue = queue.Queue(maxsize=self._queue_size)
            self.worker_threads = []
            with self._state_lock:
                self._accepting = True
            self._spawn_workers(self.worker_count)
            self.worker_thread = self.worker_threads[0]
            logger.info(f"AsyncExecutionQueue initialized with {self.worker_count} workers.")
            return True

    def _process_queue(self):
        while True:
            try:
                try:
                    item = self.task_queue.get(timeout=0.25)
                except queue.Empty:
                    # Timed polling guarantees workers can observe shutdown even
                    # if a full queue prevented every wake sentinel being added.
                    with self._state_lock:
                        accepting = self._accepting
                    if not accepting:
                        return
                    continue
                task_name, func, args, kwargs = item
                logger.debug(f"Executing queued task: {task_name}")
                try:
                    func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"❌ Error in AsyncExecutionQueue task '{task_name}': {e}")
                finally:
                    self.task_queue.task_done()
            except Exception as e:
                logger.error(f"AsyncExecutionQueue worker error: {e}")

    def submit(self, task_name: str, func: Callable, *args, **kwargs) -> bool:
        """Submit a task to be executed asynchronously."""
        task = (task_name, func, args, kwargs)
        with self._state_lock:
            if not self._accepting:
                logger.error(f"AsyncExecutionQueue is shutting down; rejected task: {task_name}")
                return False
            try:
                self.task_queue.put_nowait(task)
                return True
            except queue.Full:
                pass

        is_broker_execution = task_name.startswith(("PlaceOrder_", "CloseOrder_"))
        if not is_broker_execution:
            logger.warning(f"⚠️ AsyncExecutionQueue is full! Dropping non-critical task: {task_name}")
            return False

        # Never block the scanner indefinitely. Recheck shutdown state between
        # short bounded waits so shutdown cannot strand a producer behind put().
        logger.critical(f"⚠️ AsyncExecutionQueue full; bounded wait for broker task: {task_name}")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self._state_lock:
                if not self._accepting:
                    logger.error(f"AsyncExecutionQueue shutdown rejected broker task: {task_name}")
                    return False
                try:
                    self.task_queue.put(task, timeout=0.1)
                    return True
                except queue.Full:
                    continue
        logger.critical(f"AsyncExecutionQueue saturated; broker task was not enqueued: {task_name}")
        return False

    def shutdown(self, *, drain: bool = True, timeout_seconds: float = 10.0) -> bool:
        """Stop workers predictably, optionally allowing already queued work to finish."""
        with self._shutdown_lock:
            with self._state_lock:
                accepting = self._accepting
                self._accepting = False
            if not accepting and not any(t.is_alive() for t in self.worker_threads):
                return True
            deadline = time.monotonic() + max(0.0, float(timeout_seconds))
            if drain:
                while self.task_queue.unfinished_tasks and time.monotonic() < deadline:
                    time.sleep(0.02)
            else:
                while True:
                    try:
                        item = self.task_queue.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self.task_queue.task_done()

            # Workers poll with a short timeout and exit once accepting becomes
            # false and the queue is empty. Avoid injecting sentinels here: a
            # repeated/timed-out shutdown could otherwise strand extra sentinels
            # after every worker has exited and leave unfinished-task accounting.
            live_workers = [worker for worker in self.worker_threads if worker.is_alive()]
            for worker in live_workers:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    break
                worker.join(timeout=remaining)
            stopped = not any(worker.is_alive() for worker in self.worker_threads)
            if not stopped:
                logger.warning("AsyncExecutionQueue shutdown timed out with workers still active.")
            return stopped

# Global singleton
execution_queue = AsyncExecutionQueue()
