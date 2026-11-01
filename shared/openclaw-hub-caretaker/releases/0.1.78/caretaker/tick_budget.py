"""One cooperative tick deadline; persisted progress records completed work only."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import datetime as dt
import json
import math
import os
import time
import uuid

_current = ContextVar('caretaker_tick_budget', default=None)


class TickBudgetExceeded(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    with pending.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class TickBudget:
    def __init__(self, seconds=1200, state=None, clock=None):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 7200:
            raise ValueError('max_tick_seconds_must_be_between_1_and_7200')
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.deadline = self.started + seconds
        self.seconds = seconds
        self.state = Path(state) if state is not None else None
        self.report = {'started_at': utcnow(), 'observed_at': utcnow(),
                       'status': 'RUNNING', 'running': True, 'phase': 'starting',
                       'completed_batches': []}
        self.cleanups = []

    def check(self, phase=None, minimum=0):
        if phase:
            self.report['phase'] = phase
        if self.clock() >= self.deadline or self.deadline - self.clock() < minimum:
            raise TickBudgetExceeded('tick_time_budget_exhausted')

    def timeout(self, configured, *, minimum=1, margin=5):
        self.check(minimum=minimum + margin)
        result = min(configured, math.floor(self.deadline - self.clock() - margin))
        if result < minimum:
            raise TickBudgetExceeded('tick_time_budget_exhausted')
        return result

    def attach(self, report):
        report.update(self.report)
        self.report = report
        self.persist()

    def persist(self):
        self.report['budget'] = {'limit_seconds': self.seconds,
                                 'elapsed_seconds': max(0, self.clock() - self.started),
                                 'remaining_seconds': max(0, self.deadline - self.clock())}
        if self.state is not None:
            atomic_json(self.state / 'latest.json', self.report)

    def completed(self, lane, result):
        # Called only after publication_tick returned and persisted its outcome.
        item = {key: result.get(key) for key in
                ('project', 'status', 'reason', 'batch', 'proposal_ids', 'publication', 'checkpoint')}
        item.update(lane=lane, completed_at=utcnow())
        self.report['completed_batches'].append(item)
        self.report.update(observed_at=item['completed_at'], phase='publication:' + lane,
                           running=True, status='RUNNING')
        self.persist()

    def finish(self, report, status=None):
        self.report = report
        moment = utcnow()
        report.update(observed_at=moment, completed_at=moment, running=False)
        if status:
            report['status'] = status
        elif report.get('status') == 'RUNNING':
            report['status'] = 'OK'
        self.persist()
        if self.state is not None:
            atomic_json(self.state / 'runs' / (uuid.uuid4().hex + '.json'), report)
        return report

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            # sqlite transactions have already unwound before registered closes.
            for cleanup in reversed(self.cleanups):
                cleanup()
            _current.reset(token)

    @contextmanager
    def limit_to(self, seconds):
        """Keep a pre-existing phase cap inside the single whole-tick budget."""
        previous = self.deadline
        self.deadline = min(previous, self.clock() + seconds)
        try:
            self.check()
            yield
        finally:
            self.deadline = previous


def current():
    return _current.get()


def check(phase=None, minimum=0):
    budget = current()
    if budget is not None:
        budget.check(phase, minimum)


def timeout(configured, *, minimum=1, margin=5):
    budget = current()
    return budget.timeout(configured, minimum=minimum, margin=margin) if budget else configured


def completed(lane, result):
    budget = current()
    if budget:
        budget.completed(lane, result)
