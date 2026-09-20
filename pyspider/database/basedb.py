#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Author: Binux<i@binux.com>
#         http://binux.me
# Created on 2012-08-30 17:43:49

from __future__ import unicode_literals, division, absolute_import

import logging
logger = logging.getLogger('database.basedb')

import threading

from six import itervalues
from pyspider.libs import utils


class ConnectionPool(object):
    '''
    A simple thread-safe connection pool.

    Connections are created lazily by ``connect`` factory and reused
    across callers. ``maxsize`` limits the total number of connections
    created by the pool. Always ``release`` a connection after use,
    and call ``closeall`` when the pool is no longer needed.
    '''

    def __init__(self, connect, maxsize=10):
        self._connect = connect
        self._maxsize = maxsize
        self._pool = []
        self._size = 0
        self._closed = False
        self._cond = threading.Condition()

    def get(self, timeout=None):
        '''Get a connection from pool, create one if not full.'''
        with self._cond:
            while True:
                if self._closed:
                    raise RuntimeError("connection pool is closed")
                if self._pool:
                    return self._pool.pop()
                if self._size < self._maxsize:
                    self._size += 1
                    break
                if not self._cond.wait(timeout):
                    raise RuntimeError("get connection from pool timeout")
        try:
            return self._connect()
        except Exception:
            with self._cond:
                self._size -= 1
                self._cond.notify()
            raise

    def release(self, conn):
        '''Release a connection back to pool.'''
        if conn is None:
            return
        with self._cond:
            if self._closed:
                self._size -= 1
                close = getattr(conn, 'close', None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        logger.exception("close connection error")
            else:
                self._pool.append(conn)
            self._cond.notify()

    def closeall(self):
        '''Close all pooled connections and shutdown the pool.'''
        with self._cond:
            self._closed = True
            pool, self._pool = self._pool, []
            self._size -= len(pool)
            self._cond.notify_all()
        for conn in pool:
            close = getattr(conn, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.exception("close connection error")

    @property
    def size(self):
        '''Total connections created by the pool.'''
        return self._size

    @property
    def free_size(self):
        '''Connections currently idle in the pool.'''
        return len(self._pool)


class BaseDB:

    '''
    BaseDB

    dbcur should be overwirte
    '''
    __tablename__ = None
    placeholder = '%s'
    maxlimit = -1

    @staticmethod
    def escape(string):
        return '`%s`' % string

    @property
    def dbcur(self):
        raise NotImplementedError

    def _execute(self, sql_query, values=[]):
        dbcur = self.dbcur
        try:
            dbcur.execute(sql_query, values)
        except Exception:
            # close the broken cursor to avoid cursor/connection leak
            try:
                dbcur.close()
            except Exception:
                pass
            raise
        return dbcur

    def close(self):
        '''Close underlying connection and release resources.'''
        conn = getattr(self, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("close database connection error")
            self.conn = None

    def quit(self):
        '''Alias of close, for unified component lifecycle management.'''
        self.close()

    def _select(self, tablename=None, what="*", where="", where_values=[], offset=0, limit=None):
        tablename = self.escape(tablename or self.__tablename__)
        if isinstance(what, list) or isinstance(what, tuple) or what is None:
            what = ','.join(self.escape(f) for f in what) if what else '*'

        sql_query = "SELECT %s FROM %s" % (what, tablename)
        if where:
            sql_query += " WHERE %s" % where
        if limit:
            sql_query += " LIMIT %d, %d" % (offset, limit)
        elif offset:
            sql_query += " LIMIT %d, %d" % (offset, self.maxlimit)
        logger.debug("<sql: %s>", sql_query)

        dbcur = self._execute(sql_query, where_values)
        try:
            for row in dbcur:
                yield row
        finally:
            dbcur.close()

    def _select2dic(self, tablename=None, what="*", where="", where_values=[],
                    order=None, offset=0, limit=None):
        tablename = self.escape(tablename or self.__tablename__)
        if isinstance(what, list) or isinstance(what, tuple) or what is None:
            what = ','.join(self.escape(f) for f in what) if what else '*'

        sql_query = "SELECT %s FROM %s" % (what, tablename)
        if where:
            sql_query += " WHERE %s" % where
        if order:
            sql_query += ' ORDER BY %s' % order
        if limit:
            sql_query += " LIMIT %d, %d" % (offset, limit)
        elif offset:
            sql_query += " LIMIT %d, %d" % (offset, self.maxlimit)
        logger.debug("<sql: %s>", sql_query)

        dbcur = self._execute(sql_query, where_values)

        try:
            # f[0] may return bytes type
            # https://github.com/mysql/mysql-connector-python/pull/37
            fields = [utils.text(f[0]) for f in dbcur.description]

            for row in dbcur:
                yield dict(zip(fields, row))
        finally:
            dbcur.close()

    def _replace(self, tablename=None, **values):
        tablename = self.escape(tablename or self.__tablename__)
        if values:
            _keys = ", ".join(self.escape(k) for k in values)
            _values = ", ".join([self.placeholder, ] * len(values))
            sql_query = "REPLACE INTO %s (%s) VALUES (%s)" % (tablename, _keys, _values)
        else:
            sql_query = "REPLACE INTO %s DEFAULT VALUES" % tablename
        logger.debug("<sql: %s>", sql_query)

        if values:
            dbcur = self._execute(sql_query, list(itervalues(values)))
        else:
            dbcur = self._execute(sql_query)
        try:
            return dbcur.lastrowid
        finally:
            dbcur.close()

    def _insert(self, tablename=None, **values):
        tablename = self.escape(tablename or self.__tablename__)
        if values:
            _keys = ", ".join((self.escape(k) for k in values))
            _values = ", ".join([self.placeholder, ] * len(values))
            sql_query = "INSERT INTO %s (%s) VALUES (%s)" % (tablename, _keys, _values)
        else:
            sql_query = "INSERT INTO %s DEFAULT VALUES" % tablename
        logger.debug("<sql: %s>", sql_query)

        if values:
            dbcur = self._execute(sql_query, list(itervalues(values)))
        else:
            dbcur = self._execute(sql_query)
        try:
            return dbcur.lastrowid
        finally:
            dbcur.close()

    def _update(self, tablename=None, where="1=0", where_values=[], **values):
        tablename = self.escape(tablename or self.__tablename__)
        _key_values = ", ".join([
            "%s = %s" % (self.escape(k), self.placeholder) for k in values
        ])
        sql_query = "UPDATE %s SET %s WHERE %s" % (tablename, _key_values, where)
        logger.debug("<sql: %s>", sql_query)

        return self._execute(sql_query, list(itervalues(values)) + list(where_values))

    def _delete(self, tablename=None, where="1=0", where_values=[]):
        tablename = self.escape(tablename or self.__tablename__)
        sql_query = "DELETE FROM %s" % tablename
        if where:
            sql_query += " WHERE %s" % where
        logger.debug("<sql: %s>", sql_query)

        return self._execute(sql_query, where_values)

if __name__ == "__main__":
    import sqlite3

    class DB(BaseDB):
        __tablename__ = "test"
        placeholder = "?"

        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            cursor = self.conn.cursor()
            cursor.execute(
                '''CREATE TABLE `%s` (id INTEGER PRIMARY KEY AUTOINCREMENT, name, age)'''
                % self.__tablename__
            )

        @property
        def dbcur(self):
            return self.conn.cursor()

    db = DB()
    assert db._insert(db.__tablename__, name="binux", age=23) == 1
    assert db._select(db.__tablename__, "name, age").next() == ("binux", 23)
    assert db._select2dic(db.__tablename__, "name, age").next()["name"] == "binux"
    assert db._select2dic(db.__tablename__, "name, age").next()["age"] == 23
    db._replace(db.__tablename__, id=1, age=24)
    assert db._select(db.__tablename__, "name, age").next() == (None, 24)
    db._update(db.__tablename__, "id = 1", age=16)
    assert db._select(db.__tablename__, "name, age").next() == (None, 16)
    db._delete(db.__tablename__, "id = 1")
    assert [row for row in db._select(db.__tablename__)] == []
