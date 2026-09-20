#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Created on 2026-09-20

'''
Tests for resource lifecycle bugfixes:

* database/basedb.py: thread-safe _execute, cursor cleanup, close()
* processor/project_module.py: sys.path not growing on module reload
* fetcher/tornado_fetcher.py: robots_txt_cache TTL cleanup,
  http_client closed on quit
* components quit() releases database connections
'''

from __future__ import unicode_literals, division, absolute_import

import gc
import os
import sys
import sqlite3
import threading
import time
import unittest

from pyspider.database.basedb import BaseDB
from pyspider.processor.project_module import ProjectManager

try:
    from pyspider.fetcher.tornado_fetcher import Fetcher
    HAS_TORNADO = True
except ImportError:
    Fetcher = None
    HAS_TORNADO = False


class TrackingCursor(object):
    '''Wrap a real cursor to observe close() calls.'''

    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def execute(self, *args, **kwargs):
        return self._cursor.execute(*args, **kwargs)

    def __iter__(self):
        return iter(self._cursor)

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self.closed = True
        self._cursor.close()


class SQLiteMemoryDB(BaseDB):
    __tablename__ = 'test'
    placeholder = '?'

    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.cursors = []
        cursor = self.conn.cursor()
        cursor.execute(
            'CREATE TABLE `%s` (id INTEGER PRIMARY KEY AUTOINCREMENT, name, age)'
            % self.__tablename__
        )

    @property
    def dbcur(self):
        cursor = TrackingCursor(self.conn.cursor())
        self.cursors.append(cursor)
        return cursor


class TestBaseDBLifecycle(unittest.TestCase):

    def setUp(self):
        self.db = SQLiteMemoryDB()

    def tearDown(self):
        self.db.close()

    def test_10_execute_has_lock(self):
        lock = self.db._db_lock
        self.assertIsNotNone(lock)
        # same lock returned every time
        self.assertIs(lock, self.db._db_lock)

    def test_20_concurrent_execute(self):
        def worker(n):
            for i in range(20):
                self.db._insert(name='name%d' % n, age=i)
                list(self.db._select(what='name, age'))

        threads = [threading.Thread(target=worker, args=(n, )) for n in range(8)]
        for each in threads:
            each.start()
        for each in threads:
            each.join()

        rows = list(self.db._select(what='count(*)'))
        self.assertEqual(rows[0][0], 8 * 20)

    def test_30_cursor_closed_after_select(self):
        self.db._insert(name='binux', age=23)
        rows = list(self.db._select(what='name, age'))
        self.assertEqual(rows, [('binux', 23)])
        self.assertTrue(self.db.cursors)
        self.assertTrue(all(c.closed for c in self.db.cursors))

    def test_31_cursor_closed_after_select2dic(self):
        self.db._insert(name='binux', age=23)
        rows = list(self.db._select2dic(what='name, age'))
        self.assertEqual(rows[0]['name'], 'binux')
        self.assertTrue(all(c.closed for c in self.db.cursors))

    def test_32_cursor_closed_when_generator_abandoned(self):
        for i in range(5):
            self.db._insert(name='name%d' % i, age=i)
        gen = self.db._select(what='name, age')
        next(gen)
        gen.close()
        self.assertTrue(all(c.closed for c in self.db.cursors))

    def test_33_cursor_closed_after_insert(self):
        self.db._insert(name='binux', age=23)
        self.assertTrue(all(c.closed for c in self.db.cursors))

    def test_40_close_releases_connection(self):
        self.db._insert(name='binux', age=23)
        self.db.close()
        self.assertIsNone(self.db.conn)
        # close is idempotent
        self.db.close()


class TestProjectModuleSysPath(unittest.TestCase):

    def test_build_module_does_not_grow_sys_path(self):
        script = (
            "from pyspider.libs.base_handler import BaseHandler\n"
            "class Handler(BaseHandler):\n"
            "    pass\n"
        )
        project = {
            'name': 'test_sys_path',
            'script': script,
        }
        ProjectManager.build_module(project, {'test': True})
        path_snapshot = list(sys.path)
        # reload module repeatedly, sys.path should stay unchanged
        for _ in range(5):
            ProjectManager.build_module(project, {'test': True})
        self.assertEqual(sys.path, path_snapshot)
        gc.collect()


class FakeRobotTxt(object):

    def __init__(self, mtime):
        self._mtime = mtime

    def mtime(self):
        return self._mtime


@unittest.skipUnless(HAS_TORNADO, 'tornado is not available')
class TestFetcherResourceLifecycle(unittest.TestCase):

    def setUp(self):
        self.fetcher = Fetcher(None, None, async_mode=False)

    def tearDown(self):
        try:
            self.fetcher.quit()
        except Exception:
            pass

    def test_10_clear_robot_txt_cache(self):
        now = time.time()
        self.fetcher.robots_txt_cache['fresh.example.com'] = FakeRobotTxt(now)
        self.fetcher.robots_txt_cache['expired.example.com'] = FakeRobotTxt(
            now - 2 * self.fetcher.robot_txt_age)
        # should not raise RuntimeError: dictionary changed size during iteration
        self.fetcher.clear_robot_txt_cache()
        self.assertIn('fresh.example.com', self.fetcher.robots_txt_cache)
        self.assertNotIn('expired.example.com', self.fetcher.robots_txt_cache)

    def test_20_robots_txt_cache_bounded_on_insert(self):
        class FakeResponse(object):
            body = b''

        self.fetcher.http_client.fetch = lambda *a, **kw: FakeResponse()
        self.fetcher.robots_txt_cache_size = 2
        now = time.time()
        # prefill cache with expired entries
        for i in range(2):
            self.fetcher.robots_txt_cache['old%d.example.com' % i] = FakeRobotTxt(
                now - 2 * self.fetcher.robot_txt_age)

        result = self.fetcher.ioloop.run_sync(
            lambda: self.fetcher.can_fetch('*', 'http://new.example.com/'))
        self.assertTrue(result)
        # expired entries evicted before inserting new one
        self.assertNotIn('old0.example.com', self.fetcher.robots_txt_cache)
        self.assertNotIn('old1.example.com', self.fetcher.robots_txt_cache)
        self.assertIn('new.example.com', self.fetcher.robots_txt_cache)
        self.assertLessEqual(len(self.fetcher.robots_txt_cache),
                             self.fetcher.robots_txt_cache_size)

    def test_30_quit_closes_http_client(self):
        closed = []
        origin_close = self.fetcher.http_client.close

        def close_wrapper():
            closed.append(True)
            return origin_close()

        self.fetcher.http_client.close = close_wrapper
        self.fetcher.quit()
        self.assertTrue(closed)


class TestComponentQuitReleaseResources(unittest.TestCase):

    def test_10_result_worker_quit_closes_resultdb(self):
        from pyspider.result.result_worker import ResultWorker

        class FakeResultDB(object):
            closed = False

            def close(self):
                self.closed = True

        resultdb = FakeResultDB()
        worker = ResultWorker(resultdb, None)
        worker.quit()
        self.assertTrue(worker._quit)
        self.assertTrue(resultdb.closed)

    def test_20_processor_quit_closes_projectdb(self):
        from six.moves import queue as Queue
        from pyspider.processor.processor import Processor

        class FakeProjectDB(object):
            closed = False

            def close(self):
                self.closed = True

        projectdb = FakeProjectDB()
        processor = Processor(projectdb, Queue.Queue(), Queue.Queue(),
                              Queue.Queue(), Queue.Queue(),
                              enable_projects_import=False)
        processor.quit()
        self.assertTrue(processor._quit)
        self.assertTrue(projectdb.closed)


if __name__ == '__main__':
    unittest.main()
