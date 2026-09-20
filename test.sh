#!/bin/sh
# 手动单元测试脚本：覆盖本次资源生命周期相关的 bug 修复
#   1. database/basedb.py        - 游标/连接泄漏、ConnectionPool
#   2. fetcher/tornado_fetcher.py - robots_txt_cache TTL、quit 释放 http_client
#   3. processor/project_module.py - sys.path 重复插入
#
# 用法:
#   sh test.sh            # 运行全部修复相关单元测试
#   PYTHON=python3 sh test.sh   # 指定 python 解释器

set -e
cd "$(dirname "$0")"

# 选择一个带有依赖(six/tornado)的 python
if [ -z "$PYTHON" ]; then
    for p in /opt/homebrew/bin/python3.10 python3 python; do
        if command -v "$p" >/dev/null 2>&1 && "$p" -c "import six" >/dev/null 2>&1; then
            PYTHON="$p"
            break
        fi
    done
fi
echo "using python: $PYTHON"

# fetcher 测试需要 tornado，若当前环境没有则尝试本机已有的 tornado 源码目录
if ! "$PYTHON" -c "import tornado" >/dev/null 2>&1; then
    for d in \
        /Users/dongbufan/work/ai_color/docker_work2/021_tornado/repo \
        /Users/dongbufan/Downloads/git_public_project/021_tornado/repo
    do
        if [ -d "$d/tornado" ]; then
            echo "using tornado from: $d"
            PYTHONPATH="$d${PYTHONPATH:+:$PYTHONPATH}"
            export PYTHONPATH
            break
        fi
    done
fi

if "$PYTHON" -c "import tornado" >/dev/null 2>&1; then
    echo "tornado: available"
else
    echo "tornado: NOT available, fetcher tests will be skipped"
fi

echo "==================================================="
echo " running bugfix unit tests (test_resource_lifecycle)"
echo "==================================================="
"$PYTHON" -m pytest tests/test_resource_lifecycle.py -v

echo "==================================================="
echo " running regression: basedb self-test"
echo "==================================================="
"$PYTHON" -c "
import sqlite3
from pyspider.database.basedb import BaseDB

class DB(BaseDB):
    __tablename__ = 'test'
    placeholder = '?'

    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute(
            'CREATE TABLE \`test\` (id INTEGER PRIMARY KEY AUTOINCREMENT, name, age)')

    @property
    def dbcur(self):
        return self.conn.cursor()

db = DB()
assert db._insert(db.__tablename__, name='binux', age=23) == 1
assert next(db._select(db.__tablename__, 'name, age')) == ('binux', 23)
assert next(db._select2dic(db.__tablename__, 'name, age'))['name'] == 'binux'
assert next(db._select2dic(db.__tablename__, 'name, age'))['age'] == 23
db._replace(db.__tablename__, id=1, age=24)
assert next(db._select(db.__tablename__, 'name, age')) == (None, 24)
db._update(db.__tablename__, 'id = 1', age=16)
assert next(db._select(db.__tablename__, 'name, age')) == (None, 16)
db._delete(db.__tablename__, 'id = 1')
assert [row for row in db._select(db.__tablename__)] == []
db.close()
print('basedb self-test OK')
"

echo "==================================================="
echo " ALL TESTS PASSED"
echo "==================================================="
