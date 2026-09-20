#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Unit tests for resource lifecycle bugfixes:
#   1. database/basedb.py  - cursor/connection leak, ConnectionPool
#   2. fetcher/tornado_fetcher.py - robots_txt_cache TTL, http_client close on quit
#   3. processor/project_module.py - sys.path dedup on module reload

from __future__ import unicode_literals, division, absolute_import

import os
import sys
import time
import sqlite3
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pyspider.database.basedb import BaseDB, ConnectionPool


class TestConnectionPool(unittest.TestCase):

    def make_pool(self, maxsize=2):
        created = []

        def connect():
            conn = sqlite3.connect(':memory:')
            created.append(conn)
            return conn
        return ConnectionPool(connect, maxsize=maxsize), created

    def test_10_get_release_reuse(self):
        '''released connection should be reused instead of creating new one'''
        pool, created = self.make_pool()
        conn1 = pool.get()
        pool.release(conn1)
        conn2 = pool.get()
        self.assertIs(conn1, conn2)
        self.assertEqual(len(created), 1)
        self.assertEqual(pool.size, 1)
        pool.release(conn2)
        self.assertEqual(pool.free_size, 1)
        pool.closeall()

    def test_20_maxsize_limit(self):
        '''pool should not create more than maxsize connections'''
        pool, created = self.make_pool(maxsize=1)
        conn1 = pool.get()
        # pool exhausted, get with timeout should fail
        self.assertRaises(RuntimeError, pool.get, timeout=0.1)
        self.assertEqual(len(created), 1)
        pool.release(conn1)
        conn2 = pool.get(timeout=1)
        self.assertIs(conn1, conn2)
        pool.release(conn2)
        pool.closeall()

    def test_30_closeall(self):
        '''closeall should close pooled connections and shutdown pool'''
        pool, created = self.make_pool()
        conn = pool.get()
        pool.release(conn)
        pool.closeall()
        self.assertEqual(pool.free_size, 0)
        # connection really closed
        self.assertRaises(sqlite3.ProgrammingError, conn.execute, 'select 1')
        # pool is closed
        self.assertRaises(RuntimeError, pool.get)
        # release after close should not error and close the connection
        conn2 = sqlite3.connect(':memory:')
        pool.release(conn2)
        self.assertRaises(sqlite3.ProgrammingError, conn2.execute, 'select 1')

    def test_40_factory_error(self):
        '''failed connect should not leak pool quota'''
        def bad_connect():
            raise sqlite3.OperationalError('boom')
        pool = ConnectionPool(bad_connect, maxsize=1)
        self.assertRaises(sqlite3.OperationalError, pool.get)
        self.assertEqual(pool.size, 0)
        pool.closeall()


class TrackedCursor(object):
    '''Wrap sqlite3 cursor to track close() calls.'''

    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def execute(self, *args, **kwargs):
        return self._cursor.execute(*args, **kwargs)

    def close(self):
        self.closed = True
        self._cursor.close()

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


class TrackedSQLiteDB(BaseDB):
    __tablename__ = 'test'
    placeholder = '?'

    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute(
            'CREATE TABLE `%s` (id INTEGER PRIMARY KEY AUTOINCREMENT, name, age)'
            % self.__tablename__
        )
        self.cursors = []

    @property
    def dbcur(self):
        cursor = TrackedCursor(self.conn.cursor())
        self.cursors.append(cursor)
        return cursor


class TestBaseDBExecute(unittest.TestCase):

    def setUp(self):
        self.db = TrackedSQLiteDB()

    def tearDown(self):
        self.db.close()

    def test_10_select_closes_cursor(self):
        '''_select should close cursor after iteration'''
        self.db._insert(name='binux', age=23)
        rows = list(self.db._select(what='name, age'))
        self.assertEqual(rows, [('binux', 23)])
        self.assertTrue(self.db.cursors)
        for cursor in self.db.cursors:
            self.assertTrue(cursor.closed, 'cursor not closed')

    def test_20_select2dic_closes_cursor(self):
        '''_select2dic should close cursor after iteration'''
        self.db._insert(name='binux', age=23)
        rows = list(self.db._select2dic(what='name, age'))
        self.assertEqual(rows[0]['name'], 'binux')
        for cursor in self.db.cursors:
            self.assertTrue(cursor.closed, 'cursor not closed')

    def test_30_insert_replace_close_cursor(self):
        '''_insert/_replace should close cursor and still return lastrowid'''
        rowid = self.db._insert(name='binux', age=23)
        self.assertEqual(rowid, 1)
        self.db._replace(id=1, age=24)
        for cursor in self.db.cursors:
            self.assertTrue(cursor.closed, 'cursor not closed')
        row = list(self.db._select(what='age'))[0]
        self.assertEqual(row[0], 24)

    def test_40_update_delete_still_work(self):
        '''_update/_delete should keep returning cursor (rowcount compat)'''
        self.db._insert(name='binux', age=23)
        cursor = self.db._update(where='id = 1', age=16)
        self.assertEqual(cursor.rowcount, 1)
        cursor.close()
        cursor = self.db._delete(where='id = 1')
        self.assertEqual(cursor.rowcount, 1)
        cursor.close()
        self.assertEqual(list(self.db._select()), [])

    def test_50_execute_error_closes_cursor(self):
        '''_execute should close the cursor when sql execution fails'''
        self.assertRaises(sqlite3.OperationalError,
                          self.db._execute, 'SELECT * FROM not_exists_table')
        self.assertEqual(len(self.db.cursors), 1)
        self.assertTrue(self.db.cursors[0].closed)

    def test_60_close_releases_connection(self):
        '''close/quit should release the underlying connection'''
        conn = self.db.conn
        self.db.close()
        self.assertIsNone(self.db.conn)
        self.assertRaises(sqlite3.ProgrammingError, conn.execute, 'select 1')
        # quit is an alias of close, and safe to call repeatedly
        self.db.quit()
        self.db.close()


class TestProjectModuleSysPath(unittest.TestCase):

    @classmethod
    def load_project_module(cls):
        '''Load project_module.py directly to avoid heavy package deps.'''
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), '..',
                            'pyspider', 'processor', 'project_module.py')
        spec = importlib.util.spec_from_file_location('project_module', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_10_sys_path_not_duplicated(self):
        '''repeated ensure should not grow sys.path'''
        try:
            project_module = self.load_project_module()
        except ImportError as e:
            self.skipTest('missing dependency: %s' % e)
        pyspider_path = os.path.abspath(os.path.join(
            os.path.dirname(project_module.__file__), '..'))
        # module level call already inserted the path at most once
        self.assertLessEqual(sys.path.count(pyspider_path), 1)
        path_len = len(sys.path)
        for _ in range(5):
            project_module._ensure_pyspider_in_path()
        self.assertEqual(len(sys.path), path_len)
        self.assertLessEqual(sys.path.count(pyspider_path), 1)

    def test_20_sys_path_normalized(self):
        '''non-normalized duplicate should not be inserted again'''
        try:
            project_module = self.load_project_module()
        except ImportError as e:
            self.skipTest('missing dependency: %s' % e)
        pyspider_path = os.path.abspath(os.path.join(
            os.path.dirname(project_module.__file__), '..'))
        path_len = len(sys.path)
        project_module._ensure_pyspider_in_path()
        self.assertEqual(len(sys.path), path_len)
        self.assertIn(pyspider_path, sys.path)


class TestFetcherResourceLifecycle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            from pyspider.fetcher.tornado_fetcher import Fetcher
            cls.Fetcher = Fetcher
        except ImportError as e:
            raise unittest.SkipTest('missing dependency: %s' % e)

    def setUp(self):
        self.fetcher = self.Fetcher(None, None)

    def tearDown(self):
        if not getattr(self.fetcher, '_quit', False):
            self.fetcher.quit()

    def test_10_quit_closes_http_client(self):
        '''quit should close http_client to avoid fd leak'''
        closed = []
        origin_close = self.fetcher.http_client.close

        def tracked_close():
            closed.append(1)
            return origin_close()
        self.fetcher.http_client.close = tracked_close
        self.fetcher.quit()
        self.assertTrue(closed, 'http_client.close not called on quit')
        self.assertTrue(self.fetcher._quit)

    def test_20_quit_clears_robots_txt_cache(self):
        '''quit should clear robots_txt_cache'''
        self.fetcher.robots_txt_cache['example.com'] = object()
        self.fetcher.quit()
        self.assertEqual(self.fetcher.robots_txt_cache, {})

    def test_30_clear_robot_txt_cache_expired(self):
        '''expired entries should be removed, fresh entries kept'''
        class FakeRobotTxt(object):
            def __init__(self, mtime):
                self._mtime = mtime

            def mtime(self):
                return self._mtime

        now = time.time()
        self.fetcher.robots_txt_cache = {
            'expired1.com': FakeRobotTxt(now - self.fetcher.robot_txt_age - 1),
            'expired2.com': FakeRobotTxt(now - self.fetcher.robot_txt_age - 100),
            'fresh.com': FakeRobotTxt(now),
        }
        # should not raise RuntimeError: dictionary changed size during iteration
        self.fetcher.clear_robot_txt_cache()
        self.assertNotIn('expired1.com', self.fetcher.robots_txt_cache)
        self.assertNotIn('expired2.com', self.fetcher.robots_txt_cache)
        self.assertIn('fresh.com', self.fetcher.robots_txt_cache)

    def test_40_can_fetch_refetch_expired(self):
        '''expired robots.txt should be refetched on can_fetch'''
        class FakeResponse(object):
            body = b'User-agent: *\nAllow: /'

        class FakeHttpClient(object):
            def __init__(self):
                self.fetch_count = 0

            def fetch(self, url, **kwargs):
                self.fetch_count += 1
                return FakeResponse()

            def close(self):
                pass

        fake_client = FakeHttpClient()
        self.fetcher.http_client = fake_client
        result = self.fetcher.ioloop.run_sync(
            lambda: self.fetcher.can_fetch('*', 'http://example.com/'))
        self.assertTrue(result)
        self.assertEqual(fake_client.fetch_count, 1)
        self.assertIn('example.com', self.fetcher.robots_txt_cache)

        # cached, no refetch
        self.fetcher.ioloop.run_sync(
            lambda: self.fetcher.can_fetch('*', 'http://example.com/'))
        self.assertEqual(fake_client.fetch_count, 1)

        # expire the entry, should refetch
        robot_txt = self.fetcher.robots_txt_cache['example.com']
        robot_txt.mtime = lambda: time.time() - self.fetcher.robot_txt_age - 1
        self.fetcher.ioloop.run_sync(
            lambda: self.fetcher.can_fetch('*', 'http://example.com/'))
        self.assertEqual(fake_client.fetch_count, 2)

    def test_50_can_fetch_triggers_periodic_clean(self):
        '''can_fetch should trigger TTL clean when interval elapsed'''
        class FakeResponse(object):
            body = b'User-agent: *\nAllow: /'

        class FakeHttpClient(object):
            def fetch(self, url, **kwargs):
                return FakeResponse()

            def close(self):
                pass

        class FakeRobotTxt(object):
            def mtime(self):
                return time.time() - self._fetcher.robot_txt_age - 1
        FakeRobotTxt._fetcher = self.fetcher

        self.fetcher.http_client = FakeHttpClient()
        self.fetcher.robots_txt_cache['stale.com'] = FakeRobotTxt()
        # force the clean interval to elapse
        self.fetcher._robots_txt_cache_last_clean = 0
        self.fetcher.ioloop.run_sync(
            lambda: self.fetcher.can_fetch('*', 'http://example.com/'))
        self.assertNotIn('stale.com', self.fetcher.robots_txt_cache)
        self.assertIn('example.com', self.fetcher.robots_txt_cache)


if __name__ == '__main__':
    unittest.main()
