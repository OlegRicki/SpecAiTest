"""Machine-readable collection/results for the pipeline (no browser during collection)."""
import json
import os
import re
import pytest
from pathlib import Path

_items = {}
_reports = {}
_failures = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed and call.excinfo is not None:
        report.pipeline_failure = {'type': call.excinfo.type.__name__,
                                   'message': str(call.excinfo.value)}


def pytest_collection_finish(session):
    for item in session.items:
        doc = getattr(item.obj, '__doc__', '') or ''
        if not re.search(r'TC-\d', doc) and getattr(item, 'cls', None):
            doc = item.cls.__doc__ or ''
        ids = re.findall(r'TC-(\d+(?:\.\d+)+)', doc)
        # Function docstrings are the source of IDs; names/parameter IDs are not guessed.
        _items[item.nodeid] = {'ids': sorted(set(ids)), 'llm': bool(item.get_closest_marker('llm'))}


def pytest_runtest_logreport(report):
    _reports.setdefault(report.nodeid, {})[report.when] = report.outcome
    if hasattr(report, 'pipeline_failure'):
        _failures.setdefault(report.nodeid, {})[report.when] = report.pipeline_failure


def pytest_sessionfinish(session, exitstatus):
    target = os.environ.get('PIPELINE_PYTEST_REPORT')
    if target:
        Path(target).write_text(json.dumps({
            'exitcode': int(exitstatus), 'items': _items, 'reports': _reports, 'failures': _failures,
        }, ensure_ascii=False, indent=2))
