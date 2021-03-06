import time
import math
import shelve
import atexit
import threading
from UserDict import UserDict
from datetime import datetime

from celery import log
from celery import conf
from celery import registry
from celery.log import setup_logger
from celery.exceptions import NotRegistered

TIME_UNITS = (("day", 60 * 60 * 24, lambda n: int(math.ceil(n))),
              ("hour", 60 * 60, lambda n: int(math.ceil(n))),
              ("minute", 60, lambda n: int(math.ceil(n))),
              ("second", 1, lambda n: "%.2d" % n))


def humanize_seconds(secs, prefix=""):
    """Show seconds in human form, e.g. 60 is "1 minute", 7200 is "2
    hours"."""
    for unit, divider, formatter in TIME_UNITS:
        if secs >= divider:
            w = secs / divider
            punit = w > 1 and unit+"s" or unit
            return "%s%s %s" % (prefix, formatter(w), punit)
    return "now"


class SchedulingError(Exception):
    """An error occured while scheduling a task."""


class ScheduleEntry(object):
    """An entry in the scheduler.

    :param task: The task class.
    :keyword last_run_at: The time and date when this task was last run.
    :keyword total_run_count: Total number of times this periodic task has
        been executed.

    """

    def __init__(self, name, last_run_at=None, total_run_count=None):
        self.name = name
        self.last_run_at = last_run_at or datetime.now()
        self.total_run_count = total_run_count or 0

    def next(self):
        """Returns a new instance of the same class, but with
        its date and count fields updated."""
        return self.__class__(name=self.name,
                              last_run_at=datetime.now(),
                              total_run_count=self.total_run_count + 1)

    def is_due(self, task):
        """See :meth:`celery.task.base.PeriodicTask.is_due`."""
        return task.is_due(self.last_run_at)


class Scheduler(UserDict):
    """Scheduler for periodic tasks.

    :keyword registry: The task registry to use.
    :keyword schedule: The schedule dictionary. Default is the global
        persistent schedule ``celery.beat.schedule``.
    :keyword logger: The logger to use.
    :keyword max_interval: Maximum time to sleep between re-checking the
        schedule.

    """

    def __init__(self, **kwargs):

        attr_defaults = {"registry": lambda: {},
                         "schedule": lambda: {},
                         "logger": log.get_default_logger,
                         "max_interval": conf.CELERYBEAT_MAX_LOOP_INTERVAL}

        for attr_name, attr_default_gen in attr_defaults.items():
            if attr_name in kwargs:
                attr_value = kwargs[attr_name]
            else:
                attr_value = attr_default_gen()
            setattr(self, attr_name, attr_value)

        self.cleanup()
        self.schedule_registry()

    def tick(self):
        """Run a tick, that is one iteration of the scheduler.
        Executes all due tasks."""
        remaining_times = []
        for entry in self.schedule.values():
            is_due, next_time_to_run = self.is_due(entry)
            if is_due:
                self.logger.debug("Scheduler: Sending due task %s" % (
                        entry.name))
                result = self.apply_async(entry)
                self.logger.debug("Scheduler: %s sent. id->%s" % (
                        entry.name, result.task_id))
            if next_time_to_run:
                remaining_times.append(next_time_to_run)

        return min(remaining_times + [self.max_interval])

    def get_task(self, name):
        try:
            return self.registry[name]
        except KeyError:
            raise NotRegistered(name)

    def is_due(self, entry):
        return entry.is_due(self.get_task(entry.name))

    def apply_async(self, entry):

        # Update timestamps and run counts before we actually execute,
        # so we have that done if an exception is raised (doesn't schedule
        # forever.)
        entry = self.schedule[entry.name] = entry.next()
        task = self.get_task(entry.name)

        try:
            result = task.apply_async()
        except Exception, exc:
            raise SchedulingError(
                    "Couldn't apply scheduled task %s: %s" % (
                        task.name, exc))
        return result

    def schedule_registry(self):
        """Add the current contents of the registry to the schedule."""
        periodic_tasks = self.registry.get_all_periodic()
        for name, task in self.registry.get_all_periodic().items():
            if name not in self.schedule:
                self.logger.debug(
                        "Scheduler: Adding periodic task %s to schedule" % (
                            task.name))
            self.schedule.setdefault(name, ScheduleEntry(task.name))

    def cleanup(self):
        for task_name, entry in self.schedule.items():
            if task_name not in self.registry:
                self.schedule.pop(task_name, None)

    @property
    def schedule(self):
        return self.data


class ClockService(object):
    scheduler_cls = Scheduler
    registry = registry.tasks

    def __init__(self, logger=None, is_detached=False,
            max_interval=conf.CELERYBEAT_MAX_LOOP_INTERVAL,
            schedule_filename=conf.CELERYBEAT_SCHEDULE_FILENAME):
        self.logger = logger
        self.max_interval = max_interval
        self.schedule_filename = schedule_filename
        self._shutdown = threading.Event()
        self._stopped = threading.Event()

    def start(self):
        self.logger.info("ClockService: Starting...")
        schedule = shelve.open(filename=self.schedule_filename)
        #atexit.register(schedule.close)
        scheduler = self.scheduler_cls(schedule=schedule,
                                       registry=self.registry,
                                       logger=self.logger,
                                       max_interval=self.max_interval)
        self.logger.debug("ClockService: "
            "Ticking with max interval->%s, schedule->%s" % (
                    humanize_seconds(self.max_interval),
                    self.schedule_filename))

        synced = [False]
        def _stop():
            if not synced[0]:
                self.logger.debug("ClockService: Syncing schedule to disk...")
                schedule.sync()
                schedule.close()
                synced[0] = True
                self._stopped.set()

        silence = self.max_interval < 60 and 10 or 1
        debug = log.SilenceRepeated(self.logger.debug, max_iterations=silence)

        try:
            while True:
                if self._shutdown.isSet():
                    break
                interval = scheduler.tick()
                debug("ClockService: Waking up %s." % (
                        humanize_seconds(interval, prefix="in ")))
                time.sleep(interval)
        except (KeyboardInterrupt, SystemExit):
            _stop()
        finally:
            _stop()

    def stop(self, wait=False):
        self._shutdown.set()
        wait and self._stopped.wait() # block until shutdown done.


class ClockServiceThread(threading.Thread):

    def __init__(self, *args, **kwargs):
        self.clockservice = ClockService(*args, **kwargs)
        threading.Thread.__init__(self)
        self.setDaemon(True)

    def run(self):
        self.clockservice.start()

    def stop(self):
        self.clockservice.stop(wait=True)
